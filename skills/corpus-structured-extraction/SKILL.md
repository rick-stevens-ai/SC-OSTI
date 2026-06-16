---
name: corpus-structured-extraction
description: "Turn a heterogeneous corpus of free-form reports/papers/transcripts into a structured table. LLM-judged scoring for any subjective field; regex only for unambiguous structural extraction (file paths, github URLs, version strings). Smoke-test before scaling."
when_to_use: User asks for a master inventory / summary table / aggregate across a directory of reports, papers, replications, transcripts, logs, or notebooks. Phrasings include "build a master list/table", "score the unscored X", "summarize all the Y in Z", "what tools/datasets/hardware did we use across these papers", "aggregate Q across the corpus." Also applies when ad-hoc grepping isn't going to scale (>20 files) and the user wants an artifact (CSV/MD) at the end.
version: 1
languages: [python, bash]
dependencies: [python3, urllib (stdlib), concurrent.futures (stdlib)]
---

# Corpus structured extraction

Turning a pile of free-form documents into a structured table is a recurring
ask. The right tool depends on what kind of field you're extracting:

## HARD RULE (Rick, 2026-05-31): no regex-based scoring or reporting

**For any field that involves judgment — scores, verdicts, ratings,
sentiment, quality, "did this work" — use the LLM on every document. Period.
Do NOT use regex to pull a `Coverage: N/10` string from documents that
happen to have one, then LLM-score the rest. That produces a heterogeneous
table where some scores are author-reported (and often optimistic) and others
are independently judged, with no way to tell which without reading the
source-tag column. The aggregate statistics are then misleading.**

The right shape for scoring/judgment fields is **LLM on 100% of documents,
single prompt, single rubric**. The cost is real but predictable, and the
output is consistent.

Regex is still appropriate for **unambiguous structural extraction**:
- File paths, github URLs, DOIs, arXiv IDs
- Version strings (`v2.6.3`, `python 3.11`)
- Explicit named tools/datasets/hardware (keyword presence)
- Counts of tool mentions

The distinction: regex extracts **what the document literally says**. The LLM
judges **what the document means**. Never use regex for the latter, even when
the document seems to say it explicitly.

### REINFORCEMENT (2026-06-06): regex-extracted signals that drive selection ARE judgment

The rule above is **broader than it looks**. If you regex-extract "does this
paper mention GitHub" / "does this paper have a Code Availability section" /
"does this paper cite a GEO accession" and use those flags to **pre-filter
which papers are worth deeper LLM judgment**, you are using regex for
judgment. The fact that each individual regex is high-precision is irrelevant
— the resulting flags drive selection, and the regex's blind spots get baked
into every downstream decision.

### MANDATORY PRE-FLIGHT — run before writing any extraction script

When about to write code that extracts ANY field from a corpus, answer these
THREE questions out loud (in your reply to the user OR in code comments) BEFORE
touching the editor:

1. **What am I about to extract with regex?** Name each pattern: section
   headers, URLs, accession IDs, keyword presence, citations, version strings.
2. **For each pattern, will the extracted value EVER feed a filter, ranking,
   selection, score, or tier-assignment downstream?** If the answer is "yes"
   for any one of them, **regex is NOT allowed for that field** — even if the
   regex itself is high-precision.
3. **Am I about to type a phrase like "structural extraction is fine" or
   "this is just mechanical metadata"?** If so, STOP. That is the exact
   rationalization that has bitten this skill twice (2026-05-31 REPLICATE-PROJECT
   regex Coverage scores; 2026-06-06 OSTI extract_signals.py URL/section/
   accession patterns). Re-read the HARD RULE. If the field feeds selection,
   it is judgment, full stop.

Failure case 2026-06-06 (second strike): I had this skill loaded, the HARD RULE
in memory, AND a memorized rule about regex-for-judgment from May 31. I
*still* wrote `extract_signals.py` with regex for Code/Data Availability
sections, GitHub/Zenodo URL patterns, GEO/SRA/PDB accession patterns —
justifying it as "structural extraction per HARD RULE." Rick: *"you are not
supposed to use regex."* The skill prose was insufficient; the pre-flight
checklist above is the actual fix.

Failure case: in the OSTI 24K → 1K triage I wrote `extract_signals.py` with
regex for Code/Data Availability section headers + GitHub/GitLab/Zenodo URL
patterns + GEO/SRA/PDB accession patterns, justified as "structural extraction
is fine per HARD RULE." Rick: *"you are not supposed to use regex."* The
correct shape was a cascade of LLMs:

1. **Cheap/fast triage** (e.g. CELS llama70) over full text or 4K-char prefix
   — produces binary flags: code-mentioned, data-mentioned, HPC-only,
   proprietary-pipeline, replicable yes/no/unclear. ~4-6 req/s, free.
2. **Capable extraction** (e.g. Argo Sonnet 4.6) over full text — runs only
   on triage hits. Produces structured fields: named code URLs, named data
   registries, compute-scale estimate with paper-text quote, replicability
   score with rationale.
3. **Multi-judge consensus** (e.g. sonnet + llama70 + gemma4) over Stage-2
   output for final ranking on the small finalist pool.

This drops total expensive-model calls from 65K to ~3-10K while keeping
judgment entirely in the LLM tier.

**Regex stays valid ONLY for genuinely mechanical metadata that does not feed
selection:** file paths in a manifest, fixed-schema CSV columns, log-format
parsing, version strings in error messages. If the extracted field will
appear in ANY downstream filter / ranking / selection / scoring, use an LLM.
When in doubt, use an LLM.

### Failure case that prompted the rule (REPLICATE-PROJECT, 2026-05-31)

First-pass attempt: regex pulled `Coverage: N/10` strings from 56/129 reports
that had them, LLM scored the remaining 73. Rick: *"we have a rule about no
regex based reporting and scoring."* The 56 regex scores were author-reported
(replicators scoring their own work), the 73 LLM scores were independent.
Mixing them in one mean was apples-to-oranges. The fix: re-score all 129
with the LLM, discard regex scores entirely. Mean dropped from 7.5/10 to
6.10/10 — which is the real number. The regex-extracted scores were biased
high.

## When to use this skill

Trigger phrases:
- "build a master list/table of X across these papers/reports/logs"
- "score the unscored Y"
- "aggregate Z across the corpus"
- "summarize all the W in this directory"

If the user wants a one-paper or one-file answer, this skill is overkill — just
read it directly. Threshold to break out this skill: **~20+ documents** OR
**4+ fields to extract per document**.

## Two variants — pick before you start

**Full-text variant** (the original): you have a directory of PDFs/markdown/text
files and need to extract structured fields by reading the contents. Follow
the 5-step pipeline below.

**Metadata-only screening variant**: you have a large corpus (>5k docs) where
each doc has a stable ID and a free metadata API exists (OSTI, arXiv, CrossRef,
OpenAlex, Semantic Scholar, ADS). If the field you need to extract can be
judged from title + abstract + domain codes, **skip the PDFs entirely**. Fetch
metadata via the API, then run the LLM judgment pass on metadata only. Much
faster, much cheaper, no OCR. See
`references/osti-replication-screening-2026-06-05.md` for a worked example
(67k DOE OSTI PDFs filtered for replicability without ever opening a PDF).

**Coverage-characterization variant**: you have a known id-space (e.g. recon
output enumerating N papers) and the question is "how much do we have on
disk, what's broken, what can we recover from external sources." Three-phase
pattern: Phase 1 build master from recon → Phase 2 inventory + classify
on-disk PDFs → Phase 3 stratified recoverability probe (Unpaywall + S2).
Output is a 3-table report (coverage matrix × failure breakdown ×
recoverability). See `references/corpus-coverage-characterization-2026-06-09.md`
for the worked OSTI 407K-paper example with the S2-rate-limit pitfall and
metadata-inflation anomaly flagging.

**Augmentation-not-extraction variant**: you have an EXISTING set of
structured artifacts (cards, MD reports, JSON records) and the user wants to
*add* a field — typically pulled partly from a cheap deterministic source
(SQLite from a prior extraction) and partly from LLM judgment on the source
text. The right shape is a **two-pass cascade with idempotent marker-comment
injection**: Pass 1 = SQLite-driven canonical block insertion via
`<!-- AUGMENT:X START/END -->` markers, Pass 2 = LLM fill-gap on documents
whose Pass-1 block came up empty. Re-runs replace the marker block cleanly
without touching the rest of the document. See
`references/corpus-augmentation-not-extraction-2026-06-08.md` for the worked
xCard contacts example (5,945 cards augmented; Pass 1 covered 44%, Pass 2
LLM filled the rest) and `scripts/augment_corpus_with_markers.py` for a
ready-to-modify template.

**Always check for a metadata API before OCR-ing a corpus.** Most paper/report
sources have one.

**Synthesize-from-pre-extracted-archive variant**: the user wants a HIGH-LEVEL
SYNTHESIS across a corpus (top-N ranking, "10 most pressing X", thematic
distillation) and the corpus already has per-paper structured extractions
sitting on disk from a prior LLM pass. You do NOT need to re-extract.
Discovery: search for `*_open_problems.txt` / `*_summary.txt` / `*_extract.txt`
archives under the project tree (especially `SS-new/<PROJECT>/`,
`OLLIE/scratch/`, `<PROJECT>-AI-Notes-Papers/`). Theme-frequency the archive
to find the field's own dominant fault-lines, re-rank the canonical master TSV
on a domain-specific composite score, write the synthesis citing both. Anchor
in the corpus's own framing rather than your invention. Worked example: LUCID
"10 open problems in low-dose radbio" — mined the 201-paper
`HT-TEMP_open_problems.DIR` archive (2,023 enumerated problems), produced
ranked top-100 theory papers + 10 problems with math-bio formulations in 14
tool calls, zero new LLM extraction. See
`references/synthesize-from-pre-extracted-archive-2026-06-13.md`.

**Taxonomy-build variant**: you're not extracting fields from documents — you're
building the LABEL SPACE for a downstream classifier by merging community-source
taxonomies (arXiv + bioRxiv + medRxiv + ChemRxiv + EarthArXiv + engrXiv + OSTI
DOE codes). Same family of pull-then-smoke-then-scale pattern, different output
shape (no per-document table, instead a merged supergroup → discipline → leaf
JSON + .md). Each of the seven sources has its own non-obvious extraction quirk
(arXiv = HTML scrape with a sibling-div regex, bioRxiv = multi-window API
sample, ChemRxiv = Cloudflare-walled must curate, EarthArXiv OSF API returns
generic tree not earth-only, engrXiv `parents_count` field is absent use
`child_count > 0`, OSTI has no taxonomy endpoint sample `subjects[]` and
extract the `NN NAME` numbered prefixes). See
`references/taxonomy-merge-from-community-sources-2026-06-09.md` for the full
recipe with verified curl/regex/pagination snippets per source, the 10-super-
group merge architecture that produced 239 leaves at 95-100% source coverage,
and the explicit "where preprint servers don't cover" gap analysis.

## The pipeline (5 steps, full-text variant)

**Decide first:** is the extraction field structural or judgmental? See HARD
RULE above. The pipeline below assumes you've split the field set into:
- **Structural fields** (regex OK) — file paths, repo URLs, tool names, version strings, keyword presence
- **Judgmental fields** (LLM only, full corpus) — scores, verdicts, ratings, summaries, "did it work"

### 1. Discover and catalog

Find canonical files. A "report" or "paper" often has multiple representations
(.tex, .md, .pdf, *_v2.md, *.bak). Pick ONE per unit and score candidates so
the right one wins:

```python
def find_canonical_report(paper_dir):
    candidates = []
    for w in os.walk(paper_dir):
        depth = w[0].count(os.sep) - paper_dir.count(os.sep)
        if depth > 3: del w[1][:]; continue
        skip = {'.git','node_modules','__pycache__','venv','.venv','build','data'}
        w[1][:] = [d for d in w[1] if d not in skip]
        for f in w[2]:
            fl = f.lower()
            if 'bak' in fl or fl.startswith('.'): continue
            if not (fl.endswith('.tex') or fl.endswith('.md')): continue
            if 'report' not in fl: continue
            full = os.path.join(w[0], f)
            score = 0
            if '/report/' in full or full.endswith('/REPORT.md'): score += 10
            if fl.endswith('.tex'): score += 3
            if 'v6' in fl or 'v2' in fl: score += 1  # prefer latest version
            if 'no-go' in fl: score -= 5
            candidates.append((score, full))
    if not candidates: return None
    candidates.sort(reverse=True)
    return candidates[0][1]
```

Save the catalog to `/tmp/<project>_catalog.json` so the next step doesn't
re-walk.

### 2. Regex pass — structural fields ONLY

Use regex for **what the document literally says** about objects with stable
names: tools mentioned, datasets cited, hardware named, github URLs, version
strings, file paths, arXiv IDs. NOT for scores, verdicts, or quality
judgments — see HARD RULE.

Use a dict of keyword→regex patterns; track which docs each pattern matched.

For ambiguous tokens (`SCALE` the verb vs `SCALE` the nuclear code, `R` the
language vs random "R" in prose), anchor on **context** not just the token:

```python
'SCALE 6': r'\bSCALE[\s-]?6\b|SCALE/ORIGEN|SCALE\s+code|SCALE\s+depletion',
'R (language)': r'\bR\s+package\b|\bRStudio\b|\bCRAN\b|\bRscript\b|\bR\s+code\b',
```

Generic single-letter or common-word patterns will pollute your aggregate
counts — verify the top-30 list manually after the first pass.

### 3. LLM pass — judgmental fields on FULL corpus

For scoring, verdicts, ratings, or any field requiring judgment: run the LLM
on **every document**, single rubric, single prompt. No regex-extracted
shortcuts even when the document has an explicit score string — those are
author self-reports and mixing them with independent LLM judgments biases
the aggregate.

Save the full doc list to `/tmp/<project>_scoring_targets.json` as input.

### 4. LLM execution — parallel, with smoke test

**Smoke before scale (Rick's standing rule).** Pick 3 diverse documents,
run the LLM extraction on those, verify the JSON parses and the values look
reasonable, THEN scale to the full gap list.

**Run a judge bake-off before scaling beyond ~5k items.** When you're about
to spend hours of inference on a single judge, first spend ~5 minutes running
2-5 candidate judges against the SAME 50-item smoke set. You learn:
1. Which models can do the task at all (reasoning models often abstain — see pitfall below)
2. Which judge agrees with your trusted baseline (typically Argo Sonnet 4.6)
3. The actual speed difference (often a free local CELS model ties an Argo model at 3-4× the speed)

See `scripts/judge_bakeoff.py` for a ready-to-run template that fans the
same prompt across N judges and reports verdict distribution, pairwise
agreement, and avg latency. Pick the cheapest model that agrees with
baseline on >95% of REPLICABLE-vs-NEEDS-style binary calls.

Use the cheapest model that gets the job right. Candidates ordered by typical
production-judge utility:
- **CELS llama70** (`llama70` on cels-llama70) — local, free, ~4 req/s. 100%
  agreement with Argo Sonnet 4.6 on three-way replicability classification
  in the 50-paper bake-off (2026-06-05). First-choice production judge for
  any classification task where you'd otherwise reach for a frontier model.
- **CELS gemma4** (`gemma4`) — local, free, ~2.8 req/s, 96% agreement with
  Sonnet 4.6. Good tiebreaker when llama70 returns UNCLEAR.
- **Argo Sonnet 4.6** (`argo:claude-sonnet-4.6`) — workhorse baseline,
  0.3-2s per call, follows JSON instructions well, handles 18K-char inputs.
  Use as truth oracle for bake-off, then switch to llama70/gemma4 at scale.
- **Argo Haiku 4.5** (`argo:claude-haiku-4.5`) — faster than Sonnet, weaker at structured output.
- **Argo Opus 4.7** — overkill unless judgment is very subtle.

**DO NOT use reasoning models for short-answer classification.** Both
`kimi-k2.6` (cels-trinity) and `oss120` (cels-oss120) failed the same
50-paper task at **49/50 UNCLEAR** even at `max_tokens=1200`. They burn the
reasoning budget thinking, then output an abstention rather than commit.
This is the second project where this exact pattern has bitten — the
first was AAAR Equation Inference in May 2026 where oss120 returned UNCLEAR
on ~50% of single-letter extractions. The rule: reasoning models for
hard chain-of-thought tasks, instruction-tuned non-reasoning models
(llama70, gemma4, Sonnet) for classification.

See `scripts/llm_extract.py` for a ready-to-run template that does parallel
extraction with robust JSON parsing. Key pieces:

- **Argo endpoint** at `http://<tailnet-aggregator>:44497/v1/chat/completions` with `Authorization: Bearer stevens`.
- **8-way ThreadPoolExecutor** — Argo handles this fine; bump to 16 if you're impatient and the network is good.
- **Per-call timeout 120s, retry once on failure.**
- **3-strategy JSON parser** (strict regex → greedy brace-matching → field-by-field regex fallback) because LLMs occasionally embed `{}` inside note strings and break strict parsing.

### 5. Merge and emit

Combine regex-extracted structural fields (tools, datasets, hardware, URLs)
with LLM-extracted judgmental fields (scores, verdicts, notes). All judgment
columns come from the LLM uniformly — no source-tag needed because they all
have the same provenance.

Emit BOTH:
- **Markdown master doc** (`MASTER_X_YYYY-MM-DD.md`) — narrative, tables, histograms. Goes to `~/Dropbox/XFER/` or the project root.
- **Flat CSV** (`MASTER_X_YYYY-MM-DD.csv`) — for spreadsheet-style follow-up analysis. Same data, one row per document.

Date-stamp the filename. The user will iterate; today's master is tomorrow's
v1.

## Pitfalls

- **Regex false positives compound.** A single bad pattern in the top-30
  aggregate misleads the user about what tools "everyone uses." Always
  spot-check the top of the histogram against actual file content. Common
  traps: `SCALE` (verb), `BLAST` (rhetoric), `R` (single letter), `MPI` (only
  if `\b` anchored), `JAX` (matches "JAXA"), names that look like file
  extensions.
- **JSON-from-LLM is fragile.** Without the 3-strategy fallback parser,
  ~5-10% of Sonnet calls will fail to parse on the first try because the
  model's `note` field contains a nested `{}`. The fallback (greedy brace
  matching → field-by-field regex) recovers nearly all of these. See the
  patched extraction in `scripts/llm_extract.py`.
- **Don't ask the LLM what's already extractable.** Spend the calls on the
  hard cases. On the REPLICATE-PROJECT pass, regex got 27% for free; LLM
  filled 73%. If you LLM the full corpus, you spent 4× the tokens for the
  same answer.
- **Heterogeneous report directories often have one weird unit.** PDE
  subreports vs paper-level reports vs LUCID subreports vs BVBRC reports —
  same project, four naming conventions. Treat each subdir family as its own
  catalog pass if the depth/structure differs.
- **Don't aggregate compute numbers across docs naïvely.** If 7 reports say
  "100 GPU-h" each, the project total isn't 700 GPU-h — those are
  rebuild-from-scratch costs, not additive lifetime budgets. Report as
  "≥X GPU-h across N report-mentions (lower bound)" and tell the user where
  the real number would come from (scheduler logs, launchd accounting).
- **Smoke test isn't optional.** Skipping the 3-document smoke test cost me
  7 wasted calls (the no-JSON failures) that the smoke test would have caught
  on call 1. Sonnet's failure mode varies subtly between prompts.
- **Don't narrate the work in the final user reply.** The artifact is the
  answer. One-paragraph summary + MEDIA: links to the two files. The user
  will open the files; they don't need a story about how you regex'd things.
- **Network fetch loops need broad `except Exception`, not narrow catches.**
  `urllib.request.urlopen` can raise `http.client.RemoteDisconnected`,
  `ConnectionResetError`, `socket.timeout`, and `ssl.SSLError` — none of which
  are in `urllib.error`. A narrow `except (HTTPError, URLError, ValueError,
  TimeoutError)` lets these escape and kill threads silently inside a
  `ThreadPoolExecutor` — no output ever materializes and the script appears
  to complete normally with zero rows. Use `except Exception as e: # noqa: BLE001`
  with retry+backoff for any parallel fetch loop, and stuff `type(e).__name__`
  into the error string so you can debug after the run. The OSTI metadata
  pull learned this the hard way (12 workers triggered RemoteDisconnected,
  zero rows landed). See `references/osti-replication-screening-2026-06-05.md`.
- **Rate-limit signature is per-API.** OSTI tolerates 4 workers cleanly at
  5 req/s; 12 workers triggered server-side connection drops. Don't pick worker
  counts by feel — run a 200-record smoke at progressively higher worker counts
  and watch for `RemoteDisconnected` / 429 / connection-reset in the error
  distribution. The right number is "highest where the error rate stays at zero."
- **Python version matters for new scripts.** The default `/usr/bin/python3`
  on this M1 Mac is 3.9; `/opt/anaconda3/bin/python3` is 3.8 (which we use
  for NATS scripts because nats-py is installed there). NEITHER supports
  PEP-604 union syntax (`str | None`, `int | str`), which crashes on import
  with `TypeError: unsupported operand type(s) for |`. For new data-pipeline
  scripts that use modern typing, shebang to `python3.13` at
  `/opt/homebrew/bin/python3.13` (also present: `python3.11` at
  `~/.local/bin/python3.11`, `python3.14` at `/opt/homebrew/bin/python3`).
  Either use modern Python OR write 3.8-compatible typing (`Optional[str]`,
  `from typing import Union`). Don't mix the two by accident.
- **Naive accession-ID regexes false-positive everywhere.** PDB IDs
  (`[1-9][A-Z0-9]{3}`), GenBank (`[A-Z]{1,2}\d{5,8}`), and UniProt
  (`[A-NR-Z]\d[A-Z][A-Z0-9]{2}\d`) are so loose they match substrings of
  DOIs, dates, and journal numbers. On a 20-card OSTI sample, my unanchored
  PDB regex matched all 20 cards — because `1038`, `2026`, `1021` from DOIs
  and years look like PDB IDs. Either anchor on context (`PDB:\s*`,
  `pdb\.org/`, `rcsb\.org/structure/`) or skip these classes entirely and
  rely on URLs/DOIs. See `references/card-resolution-pattern-2026-06-05.md`.
- **HEAD 200 on a DOI is NOT proof of findability.** A DOI resolving via
  `HEAD doi.org/<doi>` only tells you the DOI is registered with a publisher.
  It does NOT distinguish "points at a deposited dataset" from "points at a
  journal article that mentions a dataset." When the question is "is the data
  findable" (not "is the citation valid"), you MUST add a registry-validation
  stage: query DataCite (`api.datacite.org/dois/<doi>`) to check
  `resourceTypeGeneral` for Dataset/Software/Collection, and query the
  specific deposit hosts (Zenodo, PRIDE, GEO, SRA, Figshare) for accessions.
  In the OSTI sample-200, raw "DOI resolves" gave 63% findability but only
  12% actually pointed at a confirmed open deposit — a 5× gap that would have
  made the report misleading without the registry pass. See
  `references/card-resolution-pattern-2026-06-05.md` Stage 4 and
  `scripts/cards_findability_pipeline.py`.
- **Filter DOI-prefix-by-registrant BEFORE any bulk OA recovery on a
  DOI-rich corpus.** Unpaywall / S2 / Crossref are journal-DOI indexes —
  they do NOT cover dataset DOIs, OSTI-internal DOIs, software registries,
  or repository-mint DOIs. On a 257K-paper OSTI recovery target,
  **147K (57%) had `10.17188/*` DOIs (LBNL Materials Project dataset entries)
  that Unpaywall 404s on uniformly** — these are real, registered DOIs,
  but Unpaywall doesn't index them because they don't point at papers.
  Without prefix-filtering the smoke test showed 2% success rate (looked
  like total failure); after dropping the 7 known OSTI-internal prefixes
  the smoke jumped to 10% (the actual recoverability of the real
  journal-DOI subset). The list of OSTI-internal prefixes to drop is:
  `10.17188` (Materials Project, dominant), `10.11578`, `10.25984`,
  `10.18141`, `10.46936`, `10.5072`, `10.18434`. Same family of trap
  applies to any DOI corpus: Zenodo dataset DOIs (`10.5281/zenodo.*`),
  Figshare (`10.6084/m9.figshare.*`), Dryad (`10.5061/dryad.*`),
  GitHub-via-Zenodo, etc. Pre-flight any DOI-based recovery: tally DOI
  registrant prefixes (`prefix = doi.split('/')[0]`) and drop any prefix
  whose domain is dataset/repository-only. The remaining "journal DOI"
  set is your real recovery candidate pool. See
  `references/unpaywall-overnight-recovery-2026-06-09.md` for the full
  worked example and the URL-rewrite trick that pushed hit rate from
  ~8% to ~10% (arxiv.org/abs → /pdf, biorxiv content → .full.pdf).
- **Measure the OA ceiling before adopting any recovery tool.** When a local
  PDF corpus has gaps and you're tempted to wrap PullR / scihub-py /
  paperscraper / unpaywall-cli, **spend 30 minutes on a 50-paper
  ceiling-measurement first** (sample → DOI lookup → host distribution →
  manual curl probe of the OA URLs). The product of (has-DOI × has-OA-URL
  × host-actually-serves-it) is your real recovery ceiling. If under ~20%,
  no tool choice changes that — every tool sources from the same S2 /
  Unpaywall / CrossRef indexes, and the publisher-side Cloudflare/403 rules
  apply equally. Pick the cheapest implementation that hits the ceiling
  (often 30 lines of direct API calls) and move on; don't compare tools
  whose differences are below the ceiling noise. Failure case 2026-06-06:
  considered adopting PullR for an 8K OSTI gap; 50-paper measurement
  showed only 16% real ceiling because escholarship.org (the dominant OA
  host for the gap papers) returns 403 to every scripted GET. PullR would
  have hit the same wall after 2-4h of integration work. The right call
  was Unpaywall as a different OA index (different green-OA coverage,
  ~25-35% ceiling) plus accepting abstract-only fallback for the rest.
  See `references/oa-recovery-ceiling-2026-06-06.md` for the worked
  measurement and the 30-minute pre-flight recipe.
- **A tool's main feature being shaped right for the problem is not the
  same as that tool being the cheapest path to the ceiling.** PullR's
  killer feature is LLM-parsing of raw citation strings. If your inputs
  are already structured metadata (title + DOI + year + journal), you
  don't need that feature — you need the ~30 lines of S2 lookup it wraps,
  which is faster to inline than to integrate.
- **Confirm the target model exists at the target proxy BEFORE writing the script.** The model registry doc (`~/.hermes/ollie-context/MODEL_REGISTRY.md`) and the live `/v1/models` endpoint drift independently. CELS endpoints rotate (`llama70`, `gemma4`, `oss120`, `trinity` have all swapped models in the last 60 days). Argo proxy adds/removes models monthly. Pattern: `curl -sS http://<proxy>/v1/models -H "Authorization: Bearer stevens" -o /tmp/models.json && python3 -c "import json; [print(m['id']) for m in json.load(open('/tmp/models.json'))['data']]"` BEFORE the first script run. If the script returns `HTTP 403 FORBIDDEN` from the proxy, it's almost always wrong model name / wrong endpoint pairing (NOT broken auth — auth would 401). Failure 2026-06-07: hardcoded `MODEL = "llama70"` against Argo's `:44497` endpoint; Argo doesn't carry llama70 today, returned 403 on every call, ~3,500 wasted records before noticing. Fix was switching to `argo:claude-haiku-4.5` which IS on Argo and is the right free fast structured-extraction workhorse (also bump `max_tokens` to 4096 when the schema's full author list could exceed 1024).
- **Dropbox 0-byte sync corruption is a recurring class.** Filenames in `ls`
  but the actual files are 0 bytes (contents never synced). Affected dirs in
  Rick's Dropbox so far: `DOE-LHP-DARIO/*.docx`, `DOE_AI_Lighthouse_Challenges.pdf`,
  many other Lighthouse/Genesis docs. Symptoms: `BadZipFile` from
  openpyxl/zipfile, `Document stream is empty` from pdftotext. Pre-flight any
  Dropbox dir with `find <dir> -type f -size +1c` to filter the shells. The
  filenames themselves are often informative — you can reconstruct taxonomy
  from filename inventory alone. See `references/osti-corpus-expansion-2026-06-07.md`
  "Dropbox 0-byte sync corruption" section for the Lighthouse-pool example
  where I reconstructed the full structure from filenames alone.
- **Don't reinvent the xCard pipeline.** When Rick asks for "more cards" /
  "re-extract cards" / "another card pass," the canonical scripts are at
  `~/Dropbox/ARGONNE-PAPERS/GOOD/` — `extract-o-matic.py` + `batch_extract.py`
  + `augment_cards.py` + `full_card_pipeline.sh`. They contain three
  card-type prompts (`_get_model_card_prompt`, `_get_agent_card_prompt`,
  `_get_data_card_prompt`) and produced the existing 4628/1231/86 DATA/MODEL/
  AGENT cards. Swap the default `gpt-4.1` to `argo:claude-sonnet-4.6` or
  `llama70` for re-runs. Full details in `references/osti-corpus-expansion-2026-06-07.md`
  "xCard generation pipeline" section.
- **Some external APIs discriminate by source IP — run from the lab host, not from home.** Government, publisher, and academic-archive APIs sometimes serve metadata to anyone but gate bulk PDF/binary downloads to institutional IPs. OSTI's `/servlets/purl/{id}` endpoint is the canonical example: returns 503/404 from home for records whose PDFs return 200 in ~310ms from `cels-oss120` (<cels-chicago-3>, ANL public range). Verified twice (the user reminded me the second time — this is a *repeat* lesson). Symptom: the API metadata pull works fine from home, then the bulk PDF/blob fetch craters with 4xx/5xx that look like server problems but are actually IP-based throttling. Pattern: scp the script to a known-good lab host (`cels-oss120` or `cels-llama70`) and run via `terminal(background=true, command='ssh cels-oss120 "..." 2>&1 | tee log')`; scp results back. `uicgpu` has NO public internet egress — compute-only, never use for fetch. Also applies to: arXiv bulk dumps, some journal-DOI redirects, and probably a lot of `.gov` repositories. When you see "API works, bulk fetch doesn't," **first check is host-network-egress, not server-side**. Specific OSTI verified-host latency table and operating pattern in `references/osti-corpus-expansion-2026-06-07.md`.
- **Cross-host scripts MUST NOT hardcode `/Users/...` or `/Volumes/...` paths.** When you write a script on m1 with `OUT = Path("/Users/stevens/code/.../recon")` and then scp it to a Linux host (cels-oss120, CherryRd), it crashes instantly on the first `mkdir` with `PermissionError: '/Users'`. Use `Path(__file__).parent / "subdir"` or `Path("~/code/...").expanduser()` instead. Pre-flight any cross-host script with `grep -nE "/Users/|/Volumes/" script.py` before the first scp — fix any hits before deploying. Bit me 2026-06-07 on the OSTI recon script; one-line fix but one wasted background-job restart.
- **Before relaunching a long-running pipeline job, ALWAYS `pgrep -af <script>` first.** When a long resumable script appears to exit (SIGTERM 143, broken pipe, terminal hangup) and you relaunch with `nohup ... &`, the original process may still be alive — and you now have two writers appending to the same output JSONL. The symptom: the line count of the output file grows past the total-records-to-process count, and when you dedupe by primary key (`osti_id`, etc.) you find ~30-40% duplicate records. Cost is mostly wasted LLM tokens but it also confuses any consumer that assumes append-only-unique semantics. The check is one line; do it every time. Failure case 2026-06-07: the original `extract_emails.py` exited with code 143 but its parent bash + a worker thread survived; relaunched, ended up with 32,005 duplicate records in `emails.jsonl` (83,725 lines for 51,756 unique osti_ids). Recovery is straightforward (dedupe by primary key at consumption time) but worth avoiding. Pattern:

  ```bash
  pgrep -af <script_name>.py | grep -v ssh | grep -v grep
  # if anything comes back, kill -9 the PIDs FIRST, then relaunch
  pkill -9 -f <script_name>.py && sleep 2 && pgrep -af <script_name>.py
  # only then:
  nohup python3 -u <script_name>.py > <script_name>.log 2>&1 < /dev/null &
  ```

  Two extra pitfalls inside this pattern: (1) `kill <pid>` returns success even if the PID didn't exist — don't trust the exit code, re-check with `pgrep` afterwards; (2) `pkill -f` matches the shell wrapper too, so the count of survivors after a kill may include the bash parent of the dead Python process — only the Python worker matters. Use `pgrep -af` with a Python-specific keyword (the script filename) and ignore any `bash -c` shell-wrapper PIDs.
- **Fetch-time "is it a PDF" check is necessary but NOT sufficient — add a downstream integrity classification pass.** Almost all PDF bulk-fetchers (including the OSTI one) check `Content-Type` contains "pdf" OR first 4 bytes are `%PDF` magic before writing to disk. This catches obvious 404 HTML and bytewise junk, but it does NOT catch: (a) truncated PDFs (header valid, body cut off mid-stream — pypdf parses but throws errors on page access), (b) HTML 302 landing pages served with `Content-Type: application/pdf` headers by misconfigured publisher servers (caught maybe 1 in 25K — rare but real), (c) OCR-only / scanned PDFs that parse cleanly but yield zero extractable text. The last one is the big one: on the OSTI corpus the OCR-only rate is **~6.5%**, which becomes thousands of papers across a 100K+ corpus, and they're invisible to the fetcher. After every bulk-fetch run, **run a separate pypdf classification pass** on a sample (200+ random PDFs) bucketing each file into: `text_ok` (parses + >60 chars text on first 2 pages), `ocr_only` (parses + zero text — needs OCR pipeline), `parse_err` (pypdf can't open at all — refetch candidate), `bad_magic` (wrong header — definitely refetch). The OCR-only bucket is its own downstream pipeline (see `ocr-and-documents` skill). The parse_err + bad_magic buckets feed back into the fetch queue. Pattern:

  ```python
  import pypdf, signal
  def handler(*a): raise TimeoutError()
  signal.signal(signal.SIGALRM, handler)
  for f in sample:
      try:
          signal.alarm(5)  # pypdf can hang on malformed xrefs
          r = pypdf.PdfReader(f, strict=False)
          if len(r.pages) == 0: bucket = "empty"
          else:
              tot = sum(len((p.extract_text() or "").strip()) for p in r.pages[:2])
              bucket = "text_ok" if tot >= 60 else "ocr_only"
          signal.alarm(0)
      except Exception:
          signal.alarm(0); bucket = "parse_err"
  ```

  The `signal.alarm` is mandatory — pypdf can spin for minutes on PDFs with malformed cross-reference tables. Without it, the sample pass hangs and you assume the script is broken. Skip files over ~20MB in the sample pass (large dataset/proceedings PDFs are valid but slow); spot-check those separately. Failure case 2026-06-07: bulk-fetched 25,886 OSTI PDFs reporting `ok` status, only noticed the 6.5% OCR-only and 1-in-25K HTML-as-PDF when Rick asked "are we checking success upon download?" The fetch-time check was passing them all as `ok`.

- **ISO date strings break `pd[-4:]` year extraction in subtle ways.** When the metadata API returns `publication_date` as `MM/DD/YYYY` (OSTI legacy format), `pd[-4:]` correctly yields the 4-digit year. But when the API returns ISO `2020-04-15T00:00:00Z` format (newer OSTI records OR some mixed-shape responses), `pd[-4:]` yields `:00Z` (the tail of the timestamp), which then gets used verbatim as a directory name — producing nonsense subdirs like `/data/2020/` AND `/data/:00Z/` for the same year. Always extract the year by **regex match** (`re.search(r"(19|20)\d{2}", pd).group(0)`) or by **parse-then-format** (`datetime.fromisoformat(pd).year`), never by string slicing. Verify on 5 random records BEFORE building the year_map — `python3 -c "import json; [print(json.loads(l).get('publication_date')) for l in open('candidates.jsonl').readlines()[:20]]"`. Failure case 2026-06-07: 23,712 OSTI PDFs landed in `unknown/` subdir (empty year field) plus 2,270 in `:00Z/` (truncated ISO tail) instead of proper year dirs. Files themselves were fine; mechanical reorg fix needed afterward. The skill `references/osti-corpus-expansion-2026-06-07.md` has the worked example.

- **LLM-extracted "single email" fields can be multi-email strings.** When an
  extractor prompts for `corresponding_email: "name@inst.edu"` from documents
  whose ground truth has multiple corresponding authors, the model will
  sometimes return the addresses concatenated with the document's literal
  separator: `"a@x.org or b@y.org"`, `"a@x.org; b@y.org"`, `"a@x.org, b@y.org"`.
  Affects ~1% of records on a 106K-paper OSTI run with Haiku 4.5; will affect
  similar fractions on any extractor where the schema asks for a single value
  but the source has multiple. The fix is a one-page regex-and-RFC-lite
  splitter that promotes the first valid address as primary, attaches the
  rest as `additional_emails`. Apply in TWO places: inline in the extractor
  (for new runs) and one-shot post-processor (for the existing corpus, so you
  don't re-pay the LLM bill). Ready-to-use both ways:
  `scripts/split_multi_email.py` (has self-test mode). Full validation
  battery + worked numbers: `references/email-validation-battery-2026-06-09.md`.
- **Validating an extracted identifier corpus — use a battery of orthogonal
  tests with priority-ordered aggregation.** When the user asks "validate the
  X we extracted from Y" (emails, DOIs, accessions, URLs, ORCIDs, ROR IDs),
  the right shape is 5-7 independent tests + a priority-ordered verdict
  function, not a single confidence score. Pin a random seed
  (`random.Random(42)`) so reruns are comparable. Distinguish **format
  defects** (extractor bug — fixable) from **deliverability/validity**
  (corpus-property, not fixable by you). Full template + the email-corpus
  worked example in `references/email-validation-battery-2026-06-09.md`.
- **SMTP RCPT TO callout verification is largely defeated by modern
  enterprise mail.** Outlook Protection, Proofpoint, Mimecast, and many
  `.edu` mailgates disconnect during EHLO or return `454` specifically to
  break address-verification probes. Of a 100-email sample probed via
  `cels-rbdgx2`: 75 SMTPServerDisconnected, 15 mail_from_454, only 2
  hard-rejected — and both of those 2 were *source-IP-policy* rejects
  (UMich TLS-strictness, CUMT blacklisting `cels.anl.gov`), not bad
  addresses. **Treat T3 as a soft signal**: weight T1+T2+T4+T5+T7 for
  validity judgment; only trust T3 when it returns a 5xx with a
  mailbox-side reason. Don't use SMTP RCPT as the deciding signal for
  any email-validity report.
- **Outbound port 25 is firewalled from m1/home networks.** Most ISP
  residential plans block outbound 25 to fight spam, so naïve SMTP probes
  time out at the configured timeout (100% UNKNOWN at 12s × 100 emails =
  9 wasted minutes). Pre-flight before scaling any SMTP-using script:
  `python3 -c "import smtplib, socket; socket.setdefaulttimeout(5);
  smtplib.SMTP('aspmx.l.google.com', 25).quit(); print('OPEN')"`. If it
  times out, run the probe via SSH to a host that has port 25 open —
  verified working on `cels-rbdgx2` (and other `cels-*` hosts). The
  pattern: embed the probe as a one-liner, ship via
  `subprocess.run(["ssh", host, "python3 -c ..."])`, parse a single
  JSON line of output. Same trap will land on any future agent-driven
  mail-verification, blocklist-check, MTA-test, or relay-probe work.
- **Web-search engines (Bing / Google / DuckDuckGo) are NOT viable as an
  identifier-validation channel without a paid Search API.** All three serve
  heavily-JS-rendered SERPs where the actual result content is loaded
  client-side and isn't in the static HTML response. Quoted-identifier
  queries (e.g. `"first.last@inst.edu"`) get the "no results found" template
  regardless of whether the identifier exists on the open web. **100%
  false-positive rate verified 2026-06-09**: a quoted email of an obviously-
  fake address returned identical hit counts to a quoted email of a real
  Argonne PI's address (both returned 6 occurrences, all template chrome:
  page title, og:tags, search-box `value=`, pagination aria-label, copilot
  link). Pre-flight test if you're tempted to add web-search to ANY
  identifier validator: run the validator with one known-good identifier
  AND one obviously-fake one; if hit counts match, scrap the method and
  reach for a real Search API (Brave Search, Serper, Google CSE). There
  is no free-scrape workaround. Full failure-mode breakdown in
  `references/email-validation-citation-databases-2026-06-09.md`.
- **Triangulate identifier validity via free citation databases instead.**
  When the question is "does this (paper_id, identifier, name) binding
  agree with reality" — emails, ORCIDs, ROR IDs, affiliations — the right
  channel is 3-source triangulation across OpenAlex (`/works/doi:<doi>`),
  Crossref (`/works/<doi>`), and the source-specific metadata API (OSTI
  `/api/v1/records/<id>`, arXiv, ADS, etc.). All free, no API key, fast
  (~200ms/call), parallel-friendly at 6+ workers, and produce **70%
  PROBABLE-or-stronger corroboration with zero LLM calls** on a 100-email
  smoke. Verdict ladder: CONFIRMED (verbatim hit on landing page) > STRONG
  (3-source agreement) > LIKELY (2-source) > PROBABLE (1-source) > WEAK
  (name only) > UNVERIFIED. Use STRONG+CONFIRMED as the high-trust pool
  for downstream outreach; PROBABLE+ for analytics. Bonus: OSTI's
  `authors[]` field also surfaces ORCIDs for ~30% of records, which are
  more valuable than the emails themselves for any contact corpus that
  needs to outlive email churn. Ready-to-run validator:
  `scripts/validate_emails_via_citation_dbs.py`. Full pattern:
  `references/email-validation-citation-databases-2026-06-09.md`.
- **For domain -> institution matching, prefer a domain-prefix-as-
  institution heuristic over a curated lookup table.** Curated
  `DOMAIN_HINTS` tables (`{"anl.gov": ["argonne"]}`) cover the 30-50 most
  common cases but fail completely on the long tail of universities
  (`princeton.edu`, `northwestern.edu`, `uchicago.edu`, ...). The fix is a
  3-tier cascade: (1) curated hints first for precision-critical cases like
  DOE labs, (2) strip generic parts (`edu`/`gov`/`cn`/`uk`/`mail`/`physics`/
  ...) from the domain and substring-match the remaining labels against the
  affiliation prose ("princeton" hits "Princeton University" trivially),
  (3) abbreviation fallback table for acronyms that don't appear in the
  spelled-out institution name (`vt`->`virginia tech`, `cup`->`china
  university of petroleum`, `utk`->`tennessee`). Boosted WEAK->PROBABLE+
  by 13/35 (37%) on the OSTI smoke. Conservative by design: false-positive
  rate stayed at zero across 100 records because affiliation prose from
  OpenAlex/Crossref/OSTI is well-formed institutional language, not free
  text. Same shape works for ROR ID -> institution, accession-prefix ->
  registry, etc. Reference implementation in
  `scripts/validate_emails_via_citation_dbs.py`.
- **Semantic Scholar anonymous tier rate-limits hard after ~400 calls and
  the failure mode is sneaky**: clean data for the first 1-2 strata
  (alphabetic processing order), then 100% http_429 for everyone else. The
  resulting per-stratum table looks like "S2 covers nothing outside the
  first two labs," which is actively misleading rather than just incomplete.
  **Rules for any bulk S2 probe**: (1) get an API key (free, ~1 day
  turnaround, `x-api-key` header) — bumps to 1 req/sec stable; OR
  (2) throttle anon to ~0.5 req/sec with a single worker; OR (3) use S2 only
  as a fallback when Unpaywall misses (cuts call volume ~40%, keeps you
  under the anon ceiling for most corpora). **Unpaywall has no equivalent
  rate-limit issue** with the required `email=` param — 8 parallel workers
  hit 25 req/sec aggregate cleanly across 3,873 probes. Default to Unpaywall
  as primary recoverability source; treat S2 as supplement. Worked case in
  `references/corpus-coverage-characterization-2026-06-09.md`.
- **Coverage-grid sanity check before treating percentages as actionable**:
  scan the OSTI-count grid (or any source-of-truth count matrix) for any
  cell that's 5-10× the row median. That's the metadata-inflation signature
  — usually bulk dataset deposits filed under `kind=Technical Report` or
  similar. Worked case 2026-06-09: LBNL 2020 had 115,917 OSTI records vs
  LBNL median ~4,500/year. Without flagging this, the LBNL coverage % (14%)
  looks like a recovery target when it's actually a metadata-cleanup target.
  Surface the anomaly in the report headline; recommend a `kind=paper`
  recon refresh before downstream work uses the inflated cell.
- **LLM enumeration of canonical codes hallucinates — sample the corpus
  instead.** When you ask an LLM "what are all the canonical X codes in the
  Y taxonomy" (DOE subject codes, ICD-10, MeSH, etc.), the model synthesizes
  from training knowledge: mixes old + new codes, duplicates entries with
  semantic overlap, invents codes that "should exist." Real failure case
  2026-06-09: llama70 returned 65 OSTI DOE codes including three different
  codes (`08`/`48`/`98`) all named `NUCLEAR DISARMAMENT`, a phantom `51
  MATERIALS SCIENCE` duplicating the real `36`, and a title-cased
  `82 Catalysis` that doesn't exist. The 46 codes that surfaced from
  sampling 5K live OSTI records were authoritative. **Rule: LLM for
  interpretation / merging / naming, empirical-corpus-sample for
  enumeration.** This is the same family as the HARD RULE — neither regex
  nor LLM-from-training-knowledge is the right authority for "what are all
  the codes in system X." The corpus is. See
  `references/llm-taxonomy-design-and-trial-2026-06-09.md`.

- **LLM defaults to uniform distribution when asked to design a taxonomy
  — override with explicit per-bucket targets.** Prompt "design an 80-leaf
  taxonomy with ~25-30 supergroups" returns 18 × 3 = 54 leaves the first
  time. Aesthetically tidy, completely unfit for power-law-distributed
  scientific output. Add HARD REQUIREMENTS: exactly N leaves, between 2 and
  5 per supergroup, coverage of the corpus's top-20 categories with named
  emphasis. After the rewrite, llama70 produced 81 leaves across 28
  supergroups with the right uneven distribution. Generalizes to any
  "design X categories of Y" prompt — override the symmetry default.
  See `references/llm-taxonomy-design-and-trial-2026-06-09.md`.

- **Numbered menu prompts induce position-index returns — never number
  classification options unless numbers ARE the expected output.** Menu
  formatted `[NN] leaf_id  -  description` causes ~40% of llama70 responses
  to come back as `"17"` (the position) instead of `"particle-physics-
  experiment"` (the leaf_id), even with explicit "output the leaf_id string"
  instructions. The `[NN]` prefix is irresistible. Fix: drop the numeric
  prefix entirely, and explicitly disclaim numbers in the instruction
  ("DO NOT output position numbers or indices — those are not leaf IDs").
  After the fix, position-index returns dropped to 0/30 in the next smoke
  batch. Applies to any classification / multi-choice / select-from-list
  LLM prompt. See `references/llm-taxonomy-design-and-trial-2026-06-09.md`.

- **Run a 5K classifier trial against every new taxonomy before declaring
  it production-ready.** A taxonomy that looks sensible on paper can still
  have dead leaves (0 hits across 5K papers), over-broad leaves (>15%
  saturation), missing categories (hallucinated leaf_ids point at them),
  or supergroups with no real corpus coverage. The trial is cheap (30-45min
  llama70 wall, free) and surfaces all four failure modes before they
  embarrass any downstream consumer. Include a hallucinated-leaf log
  (any returned `primary_id` not in the menu set) as the single most
  useful signal for "what's missing from your taxonomy." Full trial
  design + "what good looks like" thresholds in
  `references/llm-taxonomy-design-and-trial-2026-06-09.md`.

- **Read the project's canonical state documents BEFORE re-deriving from raw data.** Long-running corpus/recovery projects accumulate hand-off documents that ARE the ground truth — typically `MORNING_PICKUP.md`, `STATUS_REPORT.md`, `COVERAGE_<DATE>.md`, `*_PLAN.md`, `*_DESIGN.md`, dated `daily_deltas/*.jsonl`, or a top-level dated `.md`. When a fresh session opens with "what's the state of X" or "give me a situation report" on a project that has been running for days/weeks, the FIRST tool call after orienting is `ls -lat <project_dir>/ | head -20` to find any state docs and read the most recent ones. They will be more current AND more accurate than what you compute from raw artifacts, and they often contain numbers (paper-universe definitions, success-bucket cuts, ceiling estimates) that a fresh recount will get WRONG by mis-categorizing edge cases. Failure case 2026-06-11 OSTI status: started a 10-item full-audit todo and got 5 steps in computing a "349K gap" headline, then noticed `MORNING_PICKUP.md` and `OSTI_PAPERS_COVERAGE_2026-06-10.md` in the project dir — the real paper universe was 238K (the 407K I was about to report counted 169K Materials Project dataset entries as papers), the real ok-count on cels recovery DB was 23,613 (vs the stale 17,978 in my compacted summary), and overnight workers had delivered +19,233 PDFs. Five wasted tool calls and a wrong-by-169K headline avoided by one `ls -lat` + two `read_file` calls. Same trap will land on any multi-day project — corpus refresh, training run, build campaign, recovery pipeline. Pre-flight checklist: `(a) ls -lat <project>/*.md | head -10` for status/coverage/plan/morning docs, `(b) ls <project>/daily_deltas/ 2>/dev/null` for date-stamped per-day records, `(c) any *_<YYYY-MM-DD>.md or PICKUP/HANDOFF/STATUS-named files`. Read all matches before deriving any headline number from raw data.
- **A directory's file COUNT does not tell you what KIND of artifact lives in it — sample one filename before trusting any inherited summary.** When a previous session (or your own compacted-context handoff) says "N cards extracted to /path/cards/", do not propagate that claim to the user without `ls /path/cards/ | head -3`. The directory name "cards" is generic and gets reused — paper xCards, contact cards, model cards, agent cards, index cards all land in dirs called `cards/`. Real failure 2026-06-11: my compaction summary recorded "36,026 cards imported to UMP" as Stage 2 paper-card output. One `ls` showed `contact--bouman-purdue.edu.md`, `contact--harb-umanitoba.ca.md` — they were CONTACT cards (one per author email), and the actual paper-card corpus had grown elsewhere (MARKDOWN-DATA-CARDS went 4628→9239). Had I not sampled, the status report would have claimed Stage 2 was "done with 36K cards" when really paper-card extraction was at 36% (9239/~25K target). Rule: any cross-session inherited claim of form "N <generic-noun> at /path" needs a 1-file sample before it leaves your mouth. Cost: one `ls | head -3`. Cost of skipping: hallucinated status reports that bias next-step planning.
- **macOS `lsof` shows `(CLOSED)` on valid listening sockets when no peer is currently connected.** Wasted 3 tool calls on the multi-search server build chasing a "dead" listen socket — the python process was bound and serving fine, `lsof -nP -iTCP:<port>` just reports `(CLOSED)` until a connection comes in. `curl --max-time 3 http://127.0.0.1:<port>/health` is the authoritative liveness test for any python HTTP server you launched in background. Same on FreeBSD; doesn't happen on Linux lsof. Don't kill the server based on lsof state alone.

- **Multi-GPU OCR/inference batch runs have their own failure-mode cluster** — see `references/multi-gpu-ocr-batch-runner-2026-06-07.md` BEFORE the first N-worker launch. Three traps land every time if you skip it: (1) queue files with relative paths produce silent `FileNotFoundError` on every record because workers run from a different CWD than the `ls` that built the queue — always use `ls -d $PWD/*.pdf` for absolute paths and pre-flight with `head -1 q0.txt` (first char must be `/`); (2) cold-init CUDA race across N processes still hits one GPU even with 15s staggering — pre-warm all GPUs in a single small Python process (`for i in range(torch.cuda.device_count()): torch.zeros(4, device=f"cuda:{i}")`) BEFORE launching the workers, costs ~10s and eliminates the failure; (3) "4GB GPU mem + 0% util" is NOT a stuck worker — it's the CPU-heavy post-process phase (PDF render / image manip / markdown emit) that interleaves with GPU OCR bursts. Confirm with `ps -o pid,etime,time,cmd -p <PID>`: if CPU time grows faster than wall time, the process is multi-core crunching, leave it alone. Reference has full canonical launch sequence, per-worker logging convention (`marker_run.gpu<N>.jsonl` not shared file — JSONL append from N processes interleaves and breaks parsers), throughput numbers (75s/PDF mean on A100), and the "when this pattern does NOT apply" cases.

- **Check live host capacity (`nvidia-smi`, `df -h`, `pgrep`) before committing GPU work to a host.** When the cluster has multiple GPU-equipped hosts and you're about to commit a long batch (OCR, embedding, model serving), check each candidate host's *current* utilization before picking one — never assume a host you used yesterday is still free today. `nvidia-smi --query-gpu=index,memory.free,utilization.gpu --format=csv,noheader` over each candidate. Same logic for CPU-bound work (`uptime` + `pgrep -af python3`) and disk-bound work (`df -h <target_mount>`). Skip any host where utilization is >50% on more than one GPU OR where someone else's job is mid-flight (you don't want to OOM their work). Failure case 2026-06-07: about to commit marker-pdf OCR batch to rbdgx2 (where the OSTI PDFs already live, so it seemed natural), did the nvidia-smi check, found all 8 A100s at 96% memory + 96% utilization — someone else's job had claimed every GPU. Pivoted to uicgpu (which checked out 0/81GB used, 0% util) and added a stage-via-rsync step. Cost: 90 seconds of pre-flight, saved a likely-failed OOM run.

- **When the candidate set expands mid-run, launch a SECOND worker on the delta — do not restart the running one.** When recon discovers a much larger candidate pool (e.g. backfilling earlier years doubles the OSTI set from 174K → 404K) while bulk fetch / classifier jobs are already running on the v1 list, the temptation is to restart them pointed at v2. Don't. The running job is producing useful output and its `--resume` semantics may not survive the input swap cleanly (in-memory ID list is fixed at start; year_map mismatches; partially-written PDFs). The right move: **compute the set difference `v2 - v1` once, write it to its own `<x>_v2_additions.txt`, clone the fetcher script with patched LOG/META/IDS paths (`bulk_fetch_v2.py` → `bulk_fetch_v2.log.jsonl` etc.), launch it as a separate process.** Both workers now run in parallel against disjoint queues, sharing only the network/IO bandwidth. Merge the two log JSONLs and two output dirs at the end (trivial cat / rsync). Same pattern applies to the classifier — patch a v2-additions clone, point at `candidates_v2_additions.jsonl` with `OUT = classifications_v2add.jsonl`, run in parallel. The cost is one minute of file-juggling; the benefit is zero wasted work on what's already running and immediate forward progress on the delta. Worked example 2026-06-07: rebuilt OSTI candidate set 174K → 404K mid-fetch, spawned `bulk_fetch_v2.py` on the 229,499-ID delta and `classify_22_v2add.py` on the same delta, both ran cleanly alongside the v1 jobs with no contention. See `references/osti-corpus-expansion-2026-06-07.md` "Parallel-worker delta pattern" section.
- **Multi-source recovery fan-out: 30-sample smoke EVERY lever, even the "obviously solid" ones.**
  When the residual after a Phase 1 OA recovery splits into multiple failure
  buckets and you fan out 5-7 lever-specific workers in parallel
  (arXiv-by-DOI, free-OA-publisher templates, biblio-HTML parse, PMC, etc.),
  the temptation is to skip the smoke on levers that "seem obviously solid"
  — typically arXiv-DOI-search for a physics corpus. Don't. Concrete failure
  case 2026-06-10: launched arxiv DOI-search workers on m1 + cherryrd against
  2,170 APS/IOP/AIP DOIs, intuition said 30-50% hit. Real hit rate: **1%
  (2/200)** because arXiv's DOI metadata search is unreliable for physics
  papers (preprint and journal version often have different titles/abstracts
  and the journal DOI isn't reliably indexed in the arXiv record). The 30-
  sample smoke would have taken 2 minutes and saved 30 wall-minutes of
  arxiv workers running at 1%. Compared to the levers I DID smoke:
  free-OA-publisher templates returned 5/25 → projected 20% → actual 45%
  (exceeded smoke), biblio HTML parse returned 22/100 → 22% (matched). The
  arxiv result was 30× off intuition. **Hard rule for multi-source recovery
  fan-out: smoke EVERY candidate lever on 20-30 samples before launch, even
  the ones that seem obviously solid. Project from data, not intuition.**
  Full bucket × lever matrix, the PMC bot-wall signature (1817 bytes of
  newlines = bot detection on HTTP 200), the EuropePMC `fullTextXML`
  fallback that works where PMC PDF doesn't, the verified free-OA URL
  templates per publisher (Frontiers/Nature/PLoS/eLife/BMC/bioRxiv/Copernicus
  all confirmed), the Cloudflare-walled publisher list (PNAS/ACS/Wiley/
  Elsevier), the OA-source-memoization-per-paper rule (re-running Unpaywall
  on biblio-discovered DOIs returns 0/30 OA because the SAME paper has
  the SAME verdict regardless of which DOI you feed in), and the
  staging-then-commit pattern for cross-host workers writing into a single
  SQLite are all in `references/multi-source-pdf-recovery-fanout-2026-06-10.md`.
- **`< /dev/null` is mandatory when launching detached pipeline jobs.** A `nohup python3 -u script.py > log 2>&1 &` without `< /dev/null` keeps the launched process's stdin connected to the parent terminal. On terminal disconnect (or any session end) the process gets SIGHUP'd via the orphaned controlling-TTY, and you see `bash: tcsetattr: Inappropriate ioctl for device` in the log right before the exit-code-143. This is what caused the first `extract_emails.py` to die mid-run (3,600 / 58,445 done). The full incantation is `nohup python3 -u script.py > log 2>&1 < /dev/null &` — note the redirect of stdin, not just stdout/stderr.
- **Multi-candidate cleanup beats smarter regex for mangled identifiers.**
  When an upstream extractor mangles DOIs / URLs / accessions in a small
  number of known ways (trailing-word concat, trailing reference-number digits,
  trailing punctuation), the right pattern is: generate ~6 cleanup candidates
  per raw identifier, test each via the authoritative resolver (`HEAD doi.org`,
  `GET api.figshare.com`, etc.), first one that resolves wins. This is the
  same hard rule (no regex for judgment) applied to cleanup: regex *transforms*
  the candidates, but the *judgment* of which is real is delegated to the
  resolver's own behavior. Lifted OSTI sample-20 findability 48% → 75% with
  no smarter regex. Reference implementation in `doi_candidates()` in
  `scripts/cards_findability_pipeline.py`.
- **Diagnostic probes must share operational shape with the bulk launcher
  they're gating.** Same retry policy, same per-stage timeouts, same
  response-size cap, same rate limit. A single-attempt probe gating a
  multi-attempt bulk launcher (or a tight-cap probe gating a wide-cap
  bulk) is measuring a different upstream surface and will give you the
  wrong gate decision. Failure case 2026-06-09 OSTI refetch: single-attempt
  5MB-cap probe gave 26% recovery (gate FAIL, drafted email indicting
  upstream); 3-attempt same-cap probe on same sample gave 82% realistic
  recovery (gate PASS) — the 5MB cap had been rejecting real PDFs as
  "not_pdf" and the absence of retry was hiding transient TCP resets that
  recover cleanly. Pre-flight any probe script with the 6-item checklist
  in `references/diagnostic-probe-shape-2026-06-09.md` before scp'ing it
  to the run host.
- **MANIFEST snapshots should pre-compute joined counts and declare join\n  semantics — don't make readers do arithmetic.** When a bulk-fetch launcher\n  writes a MANIFEST that records the input dataset + sidecar id-lab map, the\n  minimum useful shape is path + SHA + row count. The *additionally useful*\n  shape adds: explicit `join_key` (so a future reader doesn't have to grep\n  the code to find which field joined the two), pre-computed `matched` count\n  (= manifest_total − unknown), and any informative summary stat from the\n  sidecar (e.g. `id_lab_map_non_numeric_count` for opaque-ID corpora).\n  Cost is ~10 lines of code, value is every future query becomes a JSON read\n  instead of a re-run. Pattern: any time you write a snapshot, ask \"what\n  derived quantity will the next reader need that I'm not pre-computing?\"\n  Failure case 2026-06-09 OSTI pilot: initial MANIFEST had path+SHA+rows but\n  not `join_key` or `matched`, so the audit-via-grep needed an external\n  cross-reference against the dry-run print to confirm the join was sound.\n  Two-line patch.\n- **`isdigit()` after dict-insert ≠ raw-row non-numeric count when source has\n  duplicate non-numeric keys.** When a sidecar id-lab map has 407,704 raw\n  rows that deduplicate to 407,704 unique keys (no dupes among unique-key\n  sense), but the audit script counts non-numeric IDs at the RAW-row level\n  (2,181) and the launcher counts them AFTER dict-insert (2,153), the 28-row\n  delta reflects within-non-numeric repeats in the source `.jsonl`. Both\n  numbers are correct; they answer slightly different questions. When\n  reporting the count in a MANIFEST, document WHICH question you answered\n  (\"non-numeric KEYS after dedup\" vs \"non-numeric ROWS in source\"). Don't\n  silently let the two diverge — the discrepancy will haunt the next audit.\n- **Wrapper-script staleness debugging — bypass the wrapper for verification.**\n  After patching a Python script that's invoked via a bash wrapper, verifying\n  the patch via the wrapper can produce stale output if the wrapper has its\n  own arg parsing, tail piping, or sub-shell layer that mangles the\n  invocation. When verifying a code change took effect, call the Python\n  interpreter directly with the same argv the wrapper would emit\n  (`/path/to/python3.10 osti_bulk_fetch.py --outdir ...`) and confirm the\n  expected output. Once the change is verified through the direct path,\n  re-run via the wrapper for the actual production launch. Failure case\n  2026-06-09: shipped a patched launcher to Aurora, invoked via wrapper.sh,\n  got empty output, briefly thought the patch hadn't propagated; direct\n  `python3.10` invocation confirmed both the ship and the patch were fine,\n  the wrapper had simply re-tail-piped on a previous MANIFEST.\n- **Probe stratification ≠ pilot stratification — compute the manifest
  distribution before proposing pilot shape.** The probe is correctly
  lab-balanced for measurement fairness (≥5 per lab so cross-lab pattern
  is visible). The pilot that follows is validating launcher mechanics
  + recovery at scale against actual production shape — a different
  question. When the actual manifest is heavily skewed (e.g. OSTI
  8,707-ID failed-recovery set: 92% LBNL, 4.8% SLAC, all other labs
  ≤1.6%), a "stratified 500-ID pilot across recoverable labs" inherited
  from probe shape spends the pilot on cross-lab signal the probe already
  established AND produces a sample that does not resemble the actual
  bulk run. Mandatory step: load the manifest, print the distribution
  along the probe's stratification axis, compare to probe distribution,
  surface any sharp divergence in the pilot recommendation. Default to
  a population-shaped pilot (straight `--limit N` off the manifest)
  unless probe was very small or recovery rates are close to the
  decision threshold. Failure case 2026-06-09 OSTI launcher dry-run:
  proposed lab-stratified 500-ID pilot framing, real manifest-shape
  pilot is 494 LBNL + 5 SLAC + 1 ORNL. Also: the deferred-labs exclusion
  (Fermi+JLab) was estimated at "500-700 IDs" from probe-era framing;
  actual manifest count is 15 IDs out of 8,707. Don't carry probe-derived
  population estimates forward without recomputing from the manifest.
  Full worked example in
  `references/diagnostic-probe-shape-2026-06-09.md` "Probe stratification
  vs population stratification" section.
- **Don't propose generic triage criteria when a prior eval report exists.**
  If the user has already replicated/evaluated a smaller cohort and now wants
  to triage a larger corpus, the eval's "where we do well / where we do
  poorly" sections ARE the criteria. Search for `*EVALUATION*.tex`,
  `REPORTS_INDEX.md`, or a `replication`/`evaluation` repo under the user's
  GitHub org BEFORE proposing options. Failure mode 2026-06-06: spent five
  turns proposing generic "random vs stratified vs hand-picked" sampling for
  OSTI 24K → 1K triage when the user had already published a 60-paper eval
  on GitHub (`rick-stevens-ai/replication-project/REPLICATION_EVALUATION_REPORT.tex`)
  with explicit success predictors (open-source code, public data,
  single-workstation compute, mainstream Python/C++ method). The user had
  to redirect me to find it. See `references/eval-report-driven-selection-2026-06-06.md`.
- **Pipeline-ran-on-sample-not-corpus is a class of bottleneck masked as
  "selection is hard."** When downstream selection uses an upstream pipeline's
  output (crosswalk joining LLM-verdicts against findability-validations),
  always check what fraction of the input universe the upstream pipeline
  actually covered before treating its output counts as the real ceiling.
  Failure case 2026-06-15 OSTI 1K selection: crosswalk_v1 joined 65,741
  paper-verdicts against findability results and produced only 12 TIER1 +
  43 TIER2 — the "bottleneck" looked like the consensus rubric being too
  strict. Actual cause: findability had only run on 486 of the 5,945 cards
  in the corpus (8% — the original development sample), with the limitation
  buried in a docstring comment. The fix wasn't methodology, it was scaling
  the findability run to the full corpus (~20-30 min wall clock on M1 at
  3 cards/s) and rerunning crosswalk_v2 against the full results. Expected
  tier1/tier2 expansion: ~10x. **Pre-flight any crosswalk / join / selection
  pipeline: `wc -l` the upstream output files and compare to the source
  universe size.** If the upstream covered <50% of the input, scale the
  upstream FIRST before tweaking the join logic. Also: search the upstream
  script for "sample" / "limit" / "lower bound" / "subset" in comments and
  variable names — the limitation is usually self-documented but easy to
  miss when you load the join script first. Bonus: **input format affects
  findability ceiling**. The OSTI .md cards (clean YAML frontmatter with
  `doi:` field) had 100% FOUND_DOI_ONLY on a 20-card smoke vs 51% on the
  same-source .txt cards with `[Not specified]` placeholders — patching
  pipeline glob from `*.txt` to `*.txt + *.md` is the unlock when the .md
  versions exist. **AND** the meta-bottleneck: after fixing coverage +
  pipeline + corpus, if the crosswalk is still short of target, compute the
  intersection rate `|upstream_cards ∩ selection_universe| / |selection_universe|`.
  When this is <10% (worked case: 726/24,388 = 3% for OSTI REPLICABLE_NO_LAB ∩
  xCards), no amount of card-pipeline tuning will hit the goal — either run
  LLM judgment on the un-carded selection-pool members directly (fast path)
  or extend the card pipeline to cover the gap (durable path). Surface the
  intersection rate explicitly before proposing fixes; without that framing
  the conversation circles back to tuning rubrics that aren't the real
  constraint. See
  `references/pipeline-coverage-gap-as-selection-bottleneck-2026-06-15.md`.

- **Don't count fallback-discovery misses against the card.** When a
  findability pipeline uses a name-search as a last-resort signal-discovery
  fallback (e.g. HF Hub model search by `model_name`), zero-hit results from
  that search should NOT bump the card into BROKEN_SIGNALS. The card never
  *claimed* the model was on HF; you went looking. The verdict counters
  should exclude fallback-discovery validators from `n_signals`. First model-
  card pass on OSTI marked 30% as BROKEN_SIGNALS purely because HF-search
  missed for niche scientific names; after excluding HF-search from the
  signal count it dropped to 12.5% (and that 12.5% is now meaningful — real
  structural signals that genuinely don't resolve). Also flag fallback HITS
  as `weak_match=True` rather than `ok=True` because they're often false
  positives (searching "fDETECT" returns unrelated `F-DetectorModel`). See
  `references/card-resolution-pattern-2026-06-05.md` "CRITICAL pitfall — HF
  name-search is a FALLBACK" section for the full reasoning.

## Verification checklist

Before declaring done:

1. `wc -l` on the CSV equals `len(catalog)` — no rows lost in the merge.
2. Sum of `cov_src == 'regex'` + `cov_src == 'llm'` + `cov_src == 'none'`
   equals total. No silent drops.
3. Top-10 of every aggregate (tools, datasets, hardware) sanity-checked
   against at least one source report. Misclassifications usually cluster at
   the top of frequency tables.
4. The markdown doc renders correctly in Telegram preview if you're going
   to send it as MEDIA: (long tables auto-rewrite to row-group bullets).

## Linked artifacts

- `scripts/llm_extract.py` — ready-to-run parallel LLM extraction template
  with the 3-strategy JSON parser. Edit the SYSTEM/USER_TMPL constants for
  your specific extraction task.
- `scripts/judge_bakeoff.py` — multi-judge bake-off template for picking
  the production LLM judge. Run BEFORE scaling beyond ~5k items. Fans the
  same prompt across N candidate models on the same smoke set, reports
  verdict distribution, pairwise agreement vs baseline, and avg latency.
  The analysis snippet is in the module docstring.
- `scripts/cards_findability_pipeline.py` — ready-to-run end-to-end
  findability pipeline (parse → smart DOI cleanup → registry validation →
  per-card verdict). Pure stdlib. Bakes in the multi-candidate DOI rehab
  pattern and the DataCite-typed-as-Dataset trick for distinguishing
  "resolves to data" from "resolves to a paper that mentions data."
  Baseline: 6 cards/s at 6× × 8× parallelism. Reuse for any
  "how findable is the data in these N cards/reports/papers" question.
- `scripts/cards_findability_code_model_pipeline.py` — sibling pipeline for
  MODEL and AGENT cards (same template, different signal set). Strong signals
  are GitHub / GitLab / HuggingFace Hub repos plus arXiv as paper-findable
  fallback. Verdict states FOUND_RUNNABLE / FOUND_PAPER_ONLY / WEAK_MATCH_ONLY
  / FOUND_DOI_ONLY / BROKEN_SIGNALS / NO_SIGNALS. Parametrized on `model` vs
  `agent` (first CLI arg). Bakes in the HF-search-as-fallback handling so
  niche-name search misses don't pollute BROKEN_SIGNALS. Baseline: 5.5 cards/s
  (models, w/ DataCite per-DOI) or 10 cards/s (agents, fewer DOIs per card).
- `scripts/augment_corpus_with_markers.py` — ready-to-modify two-pass
  augmenter for adding a field to an existing artifact corpus. Pass 1 =
  SQLite-driven canonical block injection via `<!-- AUGMENT:X START/END -->`
  markers; Pass 2 = LLM fill-gap on empty blocks. Idempotent re-runs.
  Includes one-time `.bak` backups, mixed/missing/head sample modes for
  smoke-before-scale, and parallel LLM workers with broad-except resilience.
  Customize the four CUSTOMIZE-marked sections (paths, SQLite query, LLM
  prompt, render shape) for your field. See
  `references/corpus-augmentation-not-extraction-2026-06-08.md`.
- `scripts/extract_authors_emails.py` — ready-to-run per-paper author + email
  + corresponding-author extractor for a directory of fulltext .txt files.
  Argo Haiku 4.5, 12-worker parallel, ~22 papers/s, resumable on osti_id.
  Combines LLM structured-extraction with regex belt-and-suspenders. Unlocks
  artifact-gap follow-up, replication contact lists, and author→Genesis-
  Mission-challenge-area portfolio mapping. See
  `references/osti-corpus-expansion-2026-06-07.md` "Email extraction" section.
- `scripts/validate_emails_via_citation_dbs.py` — citation-database email
  triangulation validator. Companion to the SMTP battery. Four free
  methods (OpenAlex DOI, Crossref DOI, OSTI JSON API w/ ORCID extraction,
  DOI landing-page scrape) → six-level verdict ladder (CONFIRMED / STRONG
  / LIKELY / PROBABLE / WEAK / UNVERIFIED). Pure stdlib, parallel,
  ~10 req/s aggregate at 6 workers, $0 cost. Includes the
  `affiliation_matches_domain()` 3-tier cascade for generalizing beyond
  curated domain hints. Customize the SAMPLE/OUT paths and the
  `paper_lookup` loader for your corpus. See
  `references/email-validation-citation-databases-2026-06-09.md`.
- `scripts/split_multi_email.py` — splitter for LLM-extracted "single email"
  fields that came back as multi-email strings (`"a@x.org or b@y.org"`).
  Works inline (call `split_multi_email(value)` after the LLM returns) AND
  as a one-shot post-processor (`process_corpus(src, out, diff)` rewrites an
  existing JSONL with a diff audit log). Built-in `--test` mode covers the
  five canonical edge cases. Reuse for any LLM extraction where the schema
  asks for one value but the source can have several (author emails,
  affiliation strings, ORCIDs, github URLs, …). See
  `references/email-validation-battery-2026-06-09.md`.
- `scripts/bulk_fetch_launcher_template.py` — hardened bulk-fetch launcher
  for "fetch N thousand things from a flaky external API" (OSTI PURLs,
  arXiv PDFs, registry records). Embeds the locked rules that have
  emerged across multiple OSTI/arXiv corpus projects: streaming + magic-
  byte check, configurable size cap (default 100MB) with oversize-as-recoverable bucket,
  per-stage timeouts (meta/landing/payload), 3-attempt retry with
  1s/3s/9s backoff on TRANSIENT buckets ONLY (never retry 403/404/
  terminal-html/oversize), append-only JSONL checkpoint keyed by primary
  ID for true resume, polite rate floor with --rate Hz override,
  deferred-strata exclusion knob, `--dry-run` mode that prints stratum
  distribution + first 10 IDs + wall-time estimate without fetching, and
  per-run MANIFEST.json snapshot. Customize the four CUSTOMIZE-marked
  sections (field names, stage URLs, payload magic, deferred strata) for
  your API. Built from `osti_bulk_fetch.py` 2026-06-09. **Always run
  --dry-run first and compare stratum distribution to your probe's
  stratification** — see the diagnostic-probe-shape reference for why.
  **If you extend the template with a sidecar id→stratum map** (e.g. the
  OSTI launcher's `--id-lab-map osti_id_lab_year_map.jsonl`), also extend
  the MANIFEST `paths{}` block with explicit `join_key: "<field>"`,
  `id_lab_map_sha256`, `id_lab_map_rows`, and any sidecar audit count
  (e.g. `id_lab_map_non_numeric_count`), and the `counts{}` block with
  pre-computed `matched: manifest_total - unknown_excluded`. Costs ~10
  lines, eliminates every future MANIFEST audit needing to grep code.
  See "MANIFEST snapshots should pre-compute joined counts" pitfall above.
- `scripts/stage3_uncarded_llm_judgment.py` — ready-to-run Stage-3 LLM judgment
  template for the "card coverage < 10% of selection pool" fill pattern.
  Bypasses card-findability and judges the un-carded selection-pool members
  directly from metadata + prior reviewer reasoning. Resumable on primary
  key, parallel (16 workers default), broad-except, append-only JSONL output.
  Verified shape: Argo Sonnet 4.6 at 3.5 req/s sustained / 0 errors on a
  23,662-record run, ~110min wall, $0 cost. Customize the SYS prompt
  (credibility rubric) and `build_user(rec)` (per-record metadata to expose)
  for your task. See `references/pipeline-coverage-gap-as-selection-bottleneck-2026-06-15.md`
  "Stage-3 worked recipe" for prompt design + calibration + ranking pattern.
- `scripts/wrapper_start_exit.sh` — companion bash wrapper for launching
  the bulk-fetch template (or any long-running Python pipeline) over
  two-hop SSH. Writes a START line to disk BEFORE the Python interpreter
  cold-starts, runs the work with PYTHONUNBUFFERED=1, writes an EXIT
  line + status file on completion. Solves the "empty log = process
  dead" false-positive that bit the OSTI UAN probe 3 times in a row
  (real cause: stdout buffering across the SSH chain hides early
  output for 30-60s). Polling agents check `wrapper.status`,
  `wrapper.pid`, or tail `wrapper.log` instead of the bare process
  log. Reference: kukla-self-operations SKILL.md "Pitfall: two-hop SSH
  stdout buffering" section.
- `references/argo-endpoint.md` — Argo proxy URL/auth/model-list quick
  reference (one line: copy-paste the curl one-liners).
- `references/replicate-project-2026-05-31.md` — worked example from the
  REPLICATE-PROJECT 129-report scoring pass, including the regex pattern
  library that worked. **Full-text variant.**
- `references/osti-replication-screening-2026-06-05.md` — worked example from
  the OSTI 67k-PDF replicability screening pass. **Metadata-only variant** —
  shows the OSTI API shape, rate-limit signature, the three-way verdict prompt
  that worked, the resumable JSONL pattern, and the broad-except network-fetch
  rule. Reuse this recipe for any "screen N thousand papers for property X"
  task where a metadata API exists.
- `references/card-resolution-pattern-2026-06-05.md` — the **extract → resolve
  → categorize** pattern for validating structural signals (DOIs, URLs,
  accession IDs) extracted from a card/report corpus. Documents the accession-
  regex pitfalls, publisher-paywall handling, mangled-DOI cleanup, and the
  raw-vs-cleaned findability reporting shape. Reuse for any "how findable is
  the data/code mentioned in these N cards" question.
- `references/eval-report-driven-selection-2026-06-06.md` — the **read the
  prior eval before proposing selection criteria** pattern. When the user has
  already done a small-scale evaluation of the same task class and now wants
  to triage a larger corpus, find the published eval report (GitHub
  `REPLICATION_EVALUATION_REPORT.tex` shape, or `REPORTS_INDEX.md` in the
  project root), extract its scoring rubric + "where we do well/poorly"
  sections + failure-mode taxonomy, and propose 3 selection tiers grounded
  in the eval's empirical predictors. Includes the worked OSTI 24K → 1K
  example and the structured-signals-gap diagnostic for when the upstream
  judge saw less than the rubric requires.
- `references/llm-cascade-architecture-2026-06-06.md` — the **three-stage
  cheap→capable→multi-judge cascade** for large-corpus judgment (>20K docs).
  Stage 1 = llama70 binary triage on all docs, Stage 2 = Sonnet 4.6 structured
  extraction on hits with paper-text quotes, Stage 3 = 3-judge consensus on
  finalists. Includes prompt templates for each stage, throughput/cost shape,
  the anti-pattern flag for "regex-pre-filter shortcut" that this skill
  has now been bitten by twice, **production throughput data (1.78 req/s
  sustained with 16 workers at 12KB fulltext) plus an endpoint-capacity
  probe recipe**, and the input-size-vs-signal trade table showing that
  trimming 12KB → 8KB does NOT speed Stage 1 up (output-bound) but DOES
  lose discrimination on close-call papers. Reuse for any large-corpus
  selection task where single Sonnet pass is uncomfortable.
- `references/pdf-corpus-extraction-2026-06-06.md` — the **pymupdf parallel
  extractor + empty-PDF recovery** pattern for local PDF mirrors (OSTI,
  arXiv dumps, institutional repos). Includes worker-count throughput data
  (12 workers ≈ 27 PDF/s on M1), the **curl-as-subprocess** recipe for
  publisher PURL recovery (urllib's HTTPRedirectHandler hard-fails with
  HTTP 403 on escholarship/bnl landing pages; curl with same User-Agent
  succeeds), the **pytesseract OCR fallback** recipe for image-only
  scanned PDFs (BNL/ORNL/LANL lab reports), and the HTML-landing-page
  citation_pdf_url parser. The full recipe hits 96% recovery rate on a
  100-paper smoke. Reuse for any "I have N thousand PDFs locally, extract
  text then judge" task.
- `references/osti-corpus-expansion-2026-06-07.md` — the **recon-then-fetch-
  then-fallback** pattern for *growing* an OSTI corpus (distinct from
  screening one that's already on disk). Three phases: API recon per
  (lab × year) cell to build candidate ID space → PURL bulk fetch with
  curl-not-urllib + recovery cascade → Unpaywall fallback for DOI'd gap
  → failed-list email to `comments@osti.gov`. Includes the exact 10
  DOE-SC lab name strings the API matches, the OSTI API parameter shape
  (verified 2026-06-07), the Cherry6TB 0-byte-PDF gotcha, and a
  ready-to-modify recon-script template that's resumable per cell.
- `references/genesis-mission-taxonomy-2026-06-07.md` — the **22-topic
  Genesis Mission classification axis** (21 NOFO Challenge Areas + 1 US-Japan
  addition "AI for Math and Computer Science"). DOE-IN and NNSA Lighthouse
  tracks explicitly NOT in the axis. Authoritative source for any
  IMPLICIT-MODELS or cross-corpus DOE-paper classification work. Includes
  the canonical 21-topic list verbatim, where the JSON cache lives, how to
  append topic 22, and the "stale 26 count" anti-pattern.
- `references/parallel-worker-delta-pattern-2026-06-07.md` — the **launch a
  second worker on the set-difference, don't restart the running one**
  pattern for mid-run candidate-set expansion. Includes the 7-step recipe
  (compute delta → build v2 meta → scp → clone script with patched paths →
  launch detached → merge at end), the "when this does NOT apply" edge
  cases, and the worked OSTI 174K → 404K expansion numbers. Applies to any
  embarrassingly-parallel pipeline (fetchers, classifiers, extractors) where
  the candidate set grows mid-run.
- `references/oa-recovery-ceiling-2026-06-06.md` — the **measure-the-ceiling-
  before-adopting-a-tool** pattern for gap-fill OA recovery on a large
  corpus. Worked example: 50-paper measurement on the 8K OSTI failed-
  recovery set showed PullR / direct-S2 / Unpaywall all share the same
  ~16-35% ceiling because escholarship.org Cloudflare 403s every scripted
  GET regardless of caller. Documents the 30-minute pre-flight recipe
  (sample → DOI → S2 OA URL → host tally → manual curl probe), the host
  reject-page signature (text/html + size<5000), and the rule for when
  PullR-style citation-parsing tools ARE the right call vs when to inline
  the lookup.
- `references/multi-modal-corpus-indexing-2026-06-08.md` — the **five-axis
  sidecar search layer** for atomic-card corpora. After extraction produces
  N cards (contacts, papers, entities), the consumer almost never wants only
  one retrieval mode. This reference documents the pattern: SQLite FTS5 +
  typed columns + multi-value field tables + RRF hybrid, all built from the
  same card files on disk and served behind one HTTP surface. Worked example:
  36K OSTI contact cards with exact email lookup, regex patterns, structured
  filters (lab × topic × paper_count), semantic recall, and RRF hybrid — all
  in ~12K bytes of Python. Includes the critical FTS5 tokenchars gotcha
  (default tokenizer splits emails on `@.-`), the JSON-not-YAML frontmatter
  trap, the macOS `lsof (CLOSED)` red herring, and the RRF formula with
  empirical weights.
- `references/multi-gpu-ocr-batch-runner-2026-06-07.md` — the **N-workers-
  one-per-GPU launcher pattern** for marker-pdf / Surya / any OCR or
  short-lived per-GPU-context inference batch. Covers the four pitfalls
  that bit on the first 4-GPU run (relative-path queues, cold-init CUDA
  race, "4GB+0%-util is not stuck", per-worker JSONL logs not shared),
  the canonical launch sequence including GPU pre-warm, and throughput
  numbers (~3.5× speedup at 4 workers on A100-80GB, ~13hr for 2.6K
  OCR-only PDFs). Reuse for any "I have N image-only PDFs and an
  N-GPU host" task.
- `references/corpus-augmentation-not-extraction-2026-06-08.md` — the
  **two-pass cheap-source + LLM gap-fill, with idempotent marker-comment
  injection** pattern for *augmenting* an existing artifact corpus (xCards,
  MD reports, JSON records) with an added field. Worked example: 5,945
  xCards augmented with `## Contacts (augmented)` blocks pulled first from
  a 36K-row SQLite (Pass 1, 44% coverage in 10s) then llama70 fill-gap on
  the 2,021 misses (Pass 2, ~9min @ 8 workers). Documents the marker-comment
  upsert pattern, the coverage-projection pitfall (count from the join
  table that has the actual field, not the parent metadata table), the
  pre-flight gap audit, and the filename → primary-key extraction trap
  with `PDF-N_` prefixed and `arxiv_*` named files.
- `references/email-validation-battery-2026-06-09.md` — the **seven-test
  validation battery + priority-ordered verdict** pattern for any extracted-
  identifier corpus (emails, DOIs, accessions, URLs, ORCIDs). Worked example:
  100 random emails from the OSTI 106K-paper extraction, surfaces the
  multi-email-string defect (~1% of records) and the SMTP-callout-defeated
  caveat. Includes the SSH-wrapped probe pattern for hosts where outbound
  port 25 is firewalled, and the canonical test-files layout
  (sample/run/fix/retest scripts + JSONL results + summary.md + audit diff)
  that should land in the project repo. Use as starting template any time
  the user asks "validate the X we extracted from Y."
- `references/diagnostic-probe-shape-2026-06-09.md` — the **probe must
  share operational shape with the bulk it's gating** rule. Worked
  example: OSTI failed-recovery refetch where a single-attempt 5MB-cap
  probe gave 26% recovery (gate FAIL), and a 3-attempt-with-same-cap
  probe on the same sample gave 47% first-attempt + 82% realistic
  (gate PASS) — verdict reversal driven entirely by the cap rejecting
  real big PDFs and the absence of retry hiding transient resets. Includes
  the 6-item pre-flight checklist (retry policy, timeouts, size cap,
  rate limit, fixable-vs-structural bucket classification,
  stratification axis) to run BEFORE scp'ing any probe script.
- `references/email-validation-citation-databases-2026-06-09.md` —
  **companion** to the SMTP battery, written same day. Answers the
  different question of "does the (paper_id, email, name) binding agree
  with what authoritative citation databases say" via 3-source
  triangulation (OpenAlex + Crossref + OSTI JSON API) + DOI landing-page
  scrape. Documents the web-search-engines-don't-work failure mode (with
  reproducible 100%-false-positive evidence), the affiliation-domain
  3-tier cascade heuristic that beats hand-curated tables on the long
  tail, the OSTI `authors[]` string parser (extracts name + bracketed
  affils + ORCID), and the 6-level verdict ladder. Worked example: 70/100
  PROBABLE-or-stronger, 32-second wall, $0 cost. Use whenever validation
  question is "does this person belong on this paper at this institution"
  rather than "does this mailbox exist."
- `references/corpus-coverage-characterization-2026-06-09.md` — the
  **three-phase coverage characterization** pattern (Phase 1 master from
  recon → Phase 2 inventory + classify on-disk → Phase 3 stratified
  recoverability probe). Worked example: OSTI 407,704-paper corpus across
  10 labs × 21 years (210 cells). Documents the S2-anon-rate-limit pitfall,
  the Unpaywall-as-primary recovery source rule, the metadata-inflation
  anomaly flagging (LBNL 2020 = 115,917 records single year), and the
  3-table report shape (coverage matrix × failure breakdown × recoverability).
  Reuse for any "how much of corpus X do we have, what's broken, what can
  we recover?" question. End-to-end wall ~5 min for the headline-table fast
  path, ~30 min if you wait for full PDF classification.
- `references/taxonomy-merge-from-community-sources-2026-06-09.md` — the
  **taxonomy-build variant**. When the question is "what's the label space
  for the classifier?" not "what's in this document?", you're building a
  taxonomy. Worked example: 7-source merge (arXiv 155 + bioRxiv 25 +
  medRxiv 49 + ChemRxiv 17 + EarthArXiv 25 + engrXiv 12 parents + OSTI 46
  DOE codes) into a 10-supergroup × 42-discipline × 239-leaf DOE-neutral
  taxonomy at 95-100% source coverage. Documents the seven canonical
  scientific-paper taxonomy sources with verified pull recipes per source
  (each has its own quirk: arXiv HTML scrape with sibling-div regex,
  bioRxiv multi-window API sampling, ChemRxiv Cloudflare-walled must
  curate, EarthArXiv OSF API returns generic tree, engrXiv `parents_count`
  field absent use `child_count > 0` proxy, OSTI no endpoint sample
  `subjects[]` and extract `NN NAME` numbered prefixes), the merge
  architecture, the explicit "where preprint servers don't cover" gap
  analysis (energy/nuclear/applied-engineering), and the downstream
  classifier-validation recipe. Reuse for any "build a domain classifier"
  / "neutral classification scheme" / "merge taxonomies" / "replace skewed
  classifier" request.
- `references/llm-taxonomy-design-and-trial-2026-06-09.md` — **companion**
  to the merge reference, covering the v2 rework: LLM-driven (not regex)
  extraction, target-size constraint (50-100 leaves, 25-30 supergroups),
  and the 5K classifier trial that gates production. Documents the three
  pitfalls that bit during the OSTI v1 → v2 rework (LLM hallucinates
  canonical code enumerations, LLM defaults to uniform supergroup
  distribution, numbered menu prompts induce position-index returns),
  the prompt overrides that fix each, and the "what good looks like" /
  "what bad looks like" trial-analysis tables (per-leaf distribution,
  confidence histogram, hallucinated-leaf log, supergroup balance).
  Reuse whenever a previous taxonomy build needs an LLM-driven rework
  OR before declaring any new classification scheme production-ready.
- `references/unpaywall-overnight-recovery-2026-06-09.md` — **worked
  Unpaywall recovery example** with the DOI-prefix-filter pattern that
  saved 57% of wall time (drop OSTI-internal `10.17188` and 6 sibling
  dataset prefixes before bulk launch), the deterministic URL-rewrite
  recipe for arXiv/bioRxiv/medRxiv landing pages, the probe-vs-bulk
  asymmetry that turned a "40.6% expected" into "9.8% measured" (probes
  must do real download checks, not just metadata lookup), the publisher-
  403 distribution (APS/Wiley/Elsevier are unrecoverable via Unpaywall
  alone), and the resumable SQLite state-DB shape. Reuse for any OA-source
  bulk recovery on a DOI-rich corpus.
- `references/multi-source-pdf-recovery-fanout-2026-06-10.md` — the **fan-out
  pattern** for closing the gap after Phase 1 Unpaywall recovery. Worked
  example: OSTI residual stratified into 5+ failure buckets, 7 parallel
  lever-specific workers across cels + m1 + cherryrd. Documents the bucket
  × lever matrix design, the verified free-OA publisher URL templates
  (Frontiers/Nature/PLoS/eLife/BMC/bioRxiv/Copernicus all 200-confirmed),
  the Cloudflare-walled publisher list (PNAS/ACS/Wiley/Elsevier — skip
  direct GET), the PMC bot-wall signature (1817 bytes of newlines on
  HTTP 200) and EuropePMC fullTextXML fallback, the staging-then-commit
  pattern for cross-host workers writing into shared SQLite, the
  OA-source-memoization-per-paper rule (re-running Unpaywall on
  biblio-discovered DOIs returns 0/30), the sweep-coordinator pattern
  for round-2 follow-ups on bucket-growth, and the hard rule that every
  lever needs a 20-30 sample smoke before launch — including the
  "obviously solid" ones (arxiv-by-DOI for physics intuited 30-50%,
  smoked 1%). Reuse after Phase 1 recovery whenever the residual
  decomposes into multiple failure-mode buckets and you have 24h to
  close the gap.
- `references/read-state-docs-before-rederiving-2026-06-11.md` — the
  **Phase 0: load canonical state documents before any audit** rule for
  multi-day / multi-session projects. Worked example: OSTI corpus situation-
  report request where re-deriving from `recon_v2/` jsonl files was about
  to produce a 349K-gap headline that was wrong by 169K (Materials Project
  dataset entries counted as papers), when the project root already had
  `MORNING_PICKUP.md` + `OSTI_PAPERS_COVERAGE_2026-06-10.md` with the
  correct 117K true-gap number. Includes the Phase 0 `ls -lat *.md` recipe,
  the filename patterns to look for (MORNING_PICKUP/HANDOFF/STATUS/
  COVERAGE_DATE/*_PLAN/daily_deltas), the "only recompute fast-changing
  rapidly-stale numbers" rule, the "when canonical doc absent" hand-off
  recommendation, and the discriminator between "what's the status" requests
  (read docs first) vs explicit "audit / recount / sanity-check" requests
  (recompute from raw).
- `references/synthesize-from-pre-extracted-archive-2026-06-13.md` — the
  **synthesize-from-pre-extracted-archive variant**: when the user wants
  high-level synthesis (top-N ranking, "10 most pressing X across these
  papers") and the corpus already has per-paper LLM-extracted artifacts on
  disk, mine the archive rather than re-extracting. Worked example: LUCID
  "10 open problems in low-dose radbio with math-bio formulations" delivered
  in 14 tool calls by mining the 201-paper `HT-TEMP_open_problems.DIR`
  archive (2,023 enumerated problems) + scored master TSV + existing
  synthesis docs. Documents Rick's per-project archive locations
  (`SS-new/<PROJECT>/`, `OLLIE/scratch/`, `<PROJECT>-AI-Notes-Papers/`),
  the discovery commands, the theme-frequency clustering approach, the
  composite-score ranking pattern, and the output discipline (deliverable
  goes in Notes-Papers dir, NOT /tmp/). Reuse for any "synthesize across
  Rick's research corpus" request.
