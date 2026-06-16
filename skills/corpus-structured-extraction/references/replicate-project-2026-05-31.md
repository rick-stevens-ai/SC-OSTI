# Worked example: REPLICATE-PROJECT master inventory pass

Date: 2026-05-31. Triggered by Rick asking for "master list of tools and
methods that have been used so far for replication and an estimate of tokens,
codes, data, and CPU and GPU cycles for each paper" → followed by "we need to
score the unscored replications."

## Corpus shape

- Root: `~/Dropbox/REPLICATE-PROJECT/`
- 82 top-level dirs, of which:
  - **70** are paper-level replication dirs (e.g. `1275503-COSMIC-REIONIZATION-ON-COMPUTERS/`)
  - **PDE-replications/** has 35 paper-level subdirs
  - **LUCID-replications/** has 31 paper-level subdirs
  - The rest (`common/`, `drafts/`, `scoring/`, etc.) are infrastructure, not papers
- **Total replication units: 136**, of which **129 had a canonical report file**.

Naming heterogeneity — same project, four conventions side by side:
- `<paperdir>/report/<id>_replication_report.tex` (early OSTI-numbered papers)
- `<paperdir>/report/REPORT.md` (BVBRC and 26392213-* family)
- `<paperdir>/REPORT.md` (PDE subreports, top-level pattern)
- `<paperdir>/replication/report/report.{md,tex}` (mixed)

The `find_canonical_report` scorer in the main SKILL.md picks the right file
per unit. Skip `.bak` files explicitly.

## Pre-existing artifacts (audit before starting)

- `REPORTS_INDEX.md` — covered 46 entries (mostly the early OSTI papers), stale.
- `COMPUTATIONAL_TOOLS_INVENTORY.md` — covered only 11 papers, manually written
  in April 2026. Too narrow.
- `aggregated_datasets_and_tools.pdf` — LaTeX-rendered version of similar.

These confirmed the user wanted a fresh comprehensive pass, not an update to
the existing partial doc.

## Regex pattern library that worked

The full pattern dict is ~120 entries — recorded in `/tmp/replicate_extracted_v2.json`
generator. Key categories that mattered:

- **77 tool/code patterns** spanning sim codes (LAMMPS, VASP, SCALE 6, PeleC,
  HARMPI, Dedalus, JAX-CFD, IGM, PDEBench), ML frameworks (PyTorch, TF, JAX,
  HF transformers, RDKit), LLMs (GPT-2/4, Claude, LLaMA), bioinformatics
  (BLAST+, HMMER, IQ-TREE, SPAdes, Prokka, samtools, DESeq2, BV-BRC/PATRIC),
  HPC primitives (MPI, OpenMP, CUDA, SYCL).
- **15 dataset patterns**: NCBI/GenBank, SRA, ENA, IMG/VR, Pfam, UniProt,
  PDB, MNIST, CAMELS, OEIS, ENDF, Zenodo, Figshare, HuggingFace, Materials
  Project, PubChem.
- **18 hardware patterns**: A100/H100/V100, MI250, Intel Max, uicgpu, Aurora,
  Polaris, Frontier, Summit, Theta, chiatta, cherryrd, JLSE, ALCF, OLCF,
  NERSC, CELS.

False-positive fixes mid-pass:
- `SCALE` (verb) — anchor on `SCALE 6` or `SCALE/ORIGEN` or `SCALE depletion`
- `R` (single letter) — anchor on `R package`, `RStudio`, `CRAN`, `Rscript`
- `JAX` — exclude `JAX-CFD` from the generic JAX match (separate entry)
- `Serpent` — exclude `Serpent protein` (different field)

## Score extraction — what the reports actually say

The naïve `Coverage: N/10` / `Agreement: N/10` regex caught **35/129** reports.
The other 94 used one of these forms (all caught by the extended regex):

- `Scope coverage: ~75%` (BVBRC family) → divide by 10 for /10 scale
- `Coverage score: 6/7 qualitative claims (≈85%)` (PDE family) → numerator/denominator
- `Coverage: 6/10 — central model checked` (LUCID family) → straight match but lowercase keyword
- `Agreement: 7.5/10` → round to int
- Prose-only `Agreement: strong/moderate/weak` → mapped to 8/6/3

After regex pass 2: **52 coverage, 41 agreement** extracted. The remaining
~70-80 needed LLM scoring.

## LLM scoring pass — results

- **73 reports** sent to `argo:claude-sonnet-4.6`, 8-way parallel.
- **First pass**: 66/73 succeeded. 7 returned content the strict JSON parser
  couldn't match (model embedded `{}` in the `note` field).
- **Patched parser** (3-strategy: strict → greedy brace-count → field regex)
  recovered all 7 on retry.
- **Gap-fill pass**: 19 more reports had one axis filled by regex but not the
  other — all 19 scored cleanly on first try with the patched parser.
- **Total LLM calls**: ~99 (73 + 7 retry + 19 gap-fill). At ~10K input / 200
  output per call, ~1M input tokens + 20K output. Sonnet 4.6 on Argo is free
  to us; would have been ~$3-5 on direct Anthropic at June 2026 prices.
- **Wall time**: ~3 min for the full 73-call batch, ~30s for the 19-call
  gap-fill.

## Final coverage

129/129 reports (100%) have both Coverage and Agreement scores in the master
table. Source tagged per axis (`r` = regex from report, `L` = LLM Sonnet 4.6).

Mean Coverage: 6.10/10. Mean Agreement: 6.96/10.

Verdict mix: 54 REPLICATED, 61 PARTIAL, 4 SPOT-CHECK, 9 NO-GO, 1 FAILED.

## Deliverables

- `~/Dropbox/REPLICATE-PROJECT/MASTER_TOOLS_AND_RESOURCES_2026-05-31.md` (26 KB)
- `~/Dropbox/REPLICATE-PROJECT/MASTER_SCORES_2026-05-31.csv` (43 KB)
- Both date-stamped so the next pass can be `v2`.

## Lessons (folded into the main SKILL.md)

1. Two-pass extraction (regex → LLM gap-fill) gave the right cost/coverage
   tradeoff. Doing pure-LLM would have spent 4× the tokens for the same answer.
2. The 3-strategy JSON parser is necessary, not paranoid. ~10% of Sonnet 4.6
   calls embed nested braces in `note` fields and break strict parsing.
3. Smoke-test (3 documents before scaling to 73) caught nothing this time but
   would have caught the no_json failure mode on call 1 instead of call 50.
4. Don't aggregate compute numbers across reports. The 454 GPU-h / 604 CPU-h
   total is a "sum of reported numbers" not a "project-wide budget" — report
   it honestly to the user.
