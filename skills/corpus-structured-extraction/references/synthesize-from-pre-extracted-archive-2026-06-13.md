# Synthesize-from-pre-extracted-archive pattern

**The variant captured here:** the user asks for a HIGH-LEVEL SYNTHESIS across a
corpus (top-N ranking, dominant themes, "the 10 most pressing X across these
papers"), and the corpus already has per-paper structured extractions sitting
on disk from a prior LLM pass. You do NOT need to re-extract; mine the archive
and aggregate.

**When this applies:** Rick's LUCID, REPLICATE-PROJECT, OSTI xCards, AIEN
collections all have prior per-paper extractions hiding in non-obvious
locations. Before opening a single PDF or running a single LLM call to
"summarize across these papers," search for them.

**Worked example (2026-06-13):** Rick asked for "10 most pressing open problems
in low-dose radiation biology" across the LUCID 100-paper theory subset, with
half-page summaries + math-bio formulations. Total tool calls: 14 (mostly
filesystem + 1 long writeup). Zero new LLM extraction passes. Output was
grounded in the corpus's own framing rather than my invention.

## The shape

1. **Find the canonical corpus index** — typically a scored master TSV with
   theme tags. For LUCID this is
   `~/Dropbox/REPLICATE-PROJECT/LUCID-replications/_LUCID100_ADMIN/LUCID100_SOLID_MASTER.tsv`
   (100 scored papers, columns: rank/wave/tier/priority_score/status/doi/title/
   year/venue/citationCount/themes/sources/replication_folder/pdf_or_url/
   verdict_or_plan/abstract_or_notes). Plus the wider `LUCID100_NEW_CANDIDATES.tsv`
   (275 candidates, same schema).
2. **Find pre-existing LLM extraction archives** — look for `*_open_problems.txt`,
   `*_summary.txt`, `*_extract.txt` files under the project tree, especially
   under `SS-new/`, `OLLIE/scratch/`, `lucid-hypotheses/`, or
   any `*-AI-Notes-Papers/` directory. For LUCID this was
   `~/Dropbox/SS-new/LUCID/HT-TEMP_open_problems.DIR/` — **201 files, 2,023
   enumerated problems** with structured `**OPEN PROBLEM N:** title / description /
   significance / approaches` shape from a prior extract-o-matic run. The dir
   naming convention (`HT-TEMP_*.DIR`, `*_open_problems`) is a Rick-corpus
   convention.
3. **Find any synthesis docs already written** in the project's notes
   directory. For LUCID: `~/Dropbox/LUCID-AI-Notes-Papers/` had
   `Gene Expression Responses to Ionizing Radiation_ Open Questions at Low,
   Medium, and High Doses.pdf` and `Biological Implications of Radiation
   Dose_ Low, Medium, and High Exposures.pdf` — both pre-structured
   open-questions docs that frame the field's own consensus on what's open.
4. **Mine the archive for themes** — regex the extracted problem titles for
   keyword clusters (e.g. "DSB repair pathway choice", "RBE/LET",
   "dose-rate/FLASH"), produce a frequency-by-theme table. This gives you the
   field's own ranking of importance — far more credible than your re-derivation.
   For LUCID, theme frequency revealed RBE/LET = 350 mentions (clear #1
   concern), identifiability = 198, oxidative stress = 302, cell-fate = 200.
   Use these to anchor whatever ranking the user asked for.
5. **Build the user's requested ranking** on top of the corpus index using a
   composite score (theme tags + title keywords + relevant-experimental-penalty).
   For "top theory/modeling papers" the score was: +6 if theme contains
   "computational model" or "simulation"; +3-4 per Monte Carlo / TOPAS / MEDRAS /
   PARTRAC / Geant4 / stochastic / multiscale / linear-quadratic title token;
   penalty -1 to -2 for purely experimental tokens (RNA-seq, transcriptomic,
   zebrafish, deinococcus). Dedupe by DOI across master + candidates pools.
6. **Write the deliverable grounded in the corpus** — cite specific top-ranked
   papers by `#rank` and DOI when they constrain a given problem. Do not
   invent — let the corpus's framing carry the synthesis.

## What you DON'T do

- Do NOT re-extract per-paper open problems from PDFs when the archive already
  exists. That archive cost real LLM tokens to build; reuse it.
- Do NOT run a fresh LLM pass to "rank papers by relevance to topic X" when
  the corpus's theme tags already encode the ranking signal.
- Do NOT write the synthesis from first principles. The corpus's own framing
  (2,023 problem statements across 201 papers) is more credible than yours.
  Anchor in it; cite specific extractions when you make a claim.

## Discovery commands worth memorizing

```bash
# Find LUCID-related project dirs (use any keyword for other corpora)
find ~/Dropbox -maxdepth 4 -type d -iname "*lucid*" 2>/dev/null

# Look for pre-LLM-extracted archives anywhere under the project tree
find ~/Dropbox -maxdepth 5 -type d -iname "*open_problem*" -o -iname "*extract*" 2>/dev/null

# Look for project-level scoring/master tables
find <project_dir> -maxdepth 4 -iname "*MASTER*.tsv" -o -iname "*MASTER*.csv" \
  -o -iname "*SOLID*.tsv" -o -iname "*RANKED*.tsv" 2>/dev/null

# Inspect a per-paper extraction structure (assumes standardized
# **OPEN PROBLEM N:** / **TYPE:** / **DESCRIPTION:** template)
head -50 <archive_dir>/<first_file>.txt
grep -c "^\*\*OPEN PROBLEM" <archive_dir>/*.txt | head -10
```

## Counter-cases — when this variant does NOT apply

- The corpus has no prior LLM extraction archive. Fall back to the
  metadata-only or full-text variants.
- The user wants the user's-own-judgment, not the corpus's framing
  ("YOUR top 10 picks" vs "the field's 10 open problems"). Anchor your
  ranking in your own evaluation, citing the extractions only as
  supporting evidence.
- The corpus's extractions are demonstrably stale (>1 year old AND the
  field has had a paradigm shift since). Validate freshness before
  inheriting framing.

## Output discipline (for "10 problems" / "top N ranking" deliverables)

Long-form Markdown deliverables (>10K chars) go in the project's own
Notes-Papers directory (e.g. `~/Dropbox/LUCID-AI-Notes-Papers/`), NOT
`/tmp/`. Filename pattern: `<PROJECT>-<TOPIC>-<DATE>.md`. Accompany with:
- The supporting TSV/CSV (the ranking table)
- The supporting JSON (programmatic access to the same data)
- The supporting theme-frequency JSON (provenance for ranking decisions)

So a single deliverable lands as ~4 files in the Notes-Papers dir. Telegram
delivery uses MEDIA: pointers to the markdown + the supporting TSV.

## What gets reused from the LUCID session

For any future Rick research corpus where I need to do this pattern:

1. Find the scored master + candidates TSV in `_*_ADMIN/` or
   `_*_QA/` subdir of the project.
2. Find the per-paper extraction archive under `SS-new/<PROJECT>/` or
   `OLLIE/scratch/<project>*/` (Ollie's working dir convention).
3. Find pre-existing synthesis docs in `<PROJECT>-AI-Notes-Papers/` or
   `<PROJECT>-G/`.
4. Theme-frequency the extraction archive to find dominant fault-lines.
5. Re-rank the master by domain-specific composite score for the user's
   slice.
6. Write the synthesis citing both extraction archive themes and specific
   top-ranked papers by rank+DOI.

This adds a sixth variant to the skill (full-text / metadata / coverage /
augmentation / taxonomy / **synthesize-from-pre-extracted-archive**).
