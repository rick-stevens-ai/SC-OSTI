# Using a published eval report as the selection rubric

When the user has already run a small-scale evaluation of the same task class
(e.g. "we replicated 60 papers and scored them") and now wants to pick
candidates for a larger run from a bigger corpus, **read the eval report
first** and let its findings drive your selection criteria — don't propose
generic options.

This is a sibling pattern to the smoke-then-scale rule, applied across
project iterations rather than within one. The eval report IS the smoke
test. The 24K→1K triage IS the scale-up.

## Trigger

User has:
1. An already-completed evaluation with structured scores/verdicts on a
   smaller cohort (the "training set"), AND
2. A larger corpus they want to triage for a next-stage run (the
   "deployment set"), AND
3. A vague initial ask like "what next" or "give me a sitrep" that does
   NOT spell out the selection criteria.

Wrong response: propose random/stratified sampling, ask the user to
specify criteria, or guess from domain knowledge.

Right response: **find the eval report, extract the empirical
success/failure predictors, propose triage tiers grounded in those
predictors.**

## How to find the eval report

Order of search:

1. **Published GitHub repo under the user's org.** Look for repos with
   names containing `replication`, `evaluation`, `eval`, `report`,
   `study`. `gh repo list <org> --limit 50 | grep -iE "..."`.
2. **`REPORTS_INDEX.md` / `README.md` in the project root.** Many projects
   have a curated index of canonical reports.
3. **The corpus directory itself.** Look for `*EVALUATION*.{tex,pdf,md}`,
   `*REPORT*.tex`, `MASTER_*.md` files near the corpus inputs.
4. **Recent git activity.** `git log --since='30 days ago' --name-only`
   on the project repo will surface what report files have been written
   recently.

Prefer the `.tex` source over the `.pdf` — much easier to grep for
section headings and pull out aggregate statistics tables.

## What to extract from the eval report

Three things, in this order:

### 1. The scoring rubric

The dimensions the eval measured on. Usually 1–10 scales or 3–5 verdict
categories. Find this in:

- `\subsection{Scoring rubric}` / `## Rubric` sections
- `AUDIT_PROTOCOL.md` / `SCORING_SCHEMA.md` / `scoring/SCHEMA.md`
- Per-paper score badges that follow a `\scorebadge{Coverage}{10}` or
  similar pattern — the macro definition reveals the field set

In the REPLICATE-PROJECT eval (2026-05-07, 60 papers), the rubric was two
orthogonal axes: **Coverage** (fraction of paper's contributions reproduced,
1–10) and **Agreement** (quantitative/qualitative fit to paper, 1–10).
The orthogonality matters — the rubric explicitly notes the two can
diverge (small simplification with exact numerical match = low C, high A).

### 2. The "where we do well" / "where we do poorly" sections

These name the empirical predictors of success and failure. Section
headings to grep for:

- `Where we do well` / `Where we do poorly`
- `Strongest` / `Weakest`
- `Common failure modes` / `Recurring failure modes`
- `Patterns` (after a high-score listing)
- `Predictor` / `Success signal`
- `Retry / upgrade pipeline`

These are the **selection criteria you should be using**, stated by the
person who actually ran the prior work and watched what blew up.

### 3. The four-bucket failure taxonomy

Most eval reports converge on 3–5 failure-mode buckets. Pull them out
verbatim. From REPLICATE-PROJECT:

1. **Proprietary or gated pipelines** (BV-BRC, SEEDtk)
2. **Compute scale** (10⁴–10⁵ GPU-hours)
3. **Missing code + paywalled text**
4. **PINN hyperparameter sensitivity without published code**

These translate directly to **hard excludes** for the larger candidate
pool — papers matching these patterns should be filtered out regardless
of what an upstream judge said.

## Mapping eval findings to selection tiers

The output shape that works: 3 tiers, each with explicit predicates,
ordered by signal strength.

Example from the OSTI 24K → 1K triage (2026-06-06):

**Tier 1 — Highest confidence** (~50% of target)
Conjunction of strongest signals from the eval:
- Current judge verdict is positive (REPLICABLE_NO_LAB)
- Has an osti-cards entry classified FOUND_RUNNABLE or FOUND_DEPOSIT
  (the eval said "open-source code + public data" was the #1 predictor)

**Tier 2 — Strong signal** (~30%)
Weaker but still positive signal:
- Same verdict, AND
- FOUND_DOI_ONLY (DOI resolves but unconfirmed deposit)
- Subject filter against eval's failure-mode domains

**Tier 3 — Diversity fill** (~20%)
Stratified sample across domains the eval showed high mean scores in
(CS/Graph Algorithms 9.0, Mathematics 10.0, Comp Chem 9.0).

**Hard excludes** (regardless of judge verdict):
- BROKEN_SIGNALS (DOIs that don't resolve)
- Subjects in the eval's failure buckets (BV-BRC/SEEDtk dependents,
  10⁴+ GPU-hour campaigns, PINN-without-code)

## The structured-signals gap

A frequent finding: **the upstream judge that scored the large corpus
saw less signal than the eval rubric requires.** OSTI Sonnet judge saw
only title + abstract + subjects. The eval rubric's top-4 predictors
(code availability, data availability, compute scale, method
expressibility) live mostly in Methods/Code Availability sections —
NOT in the abstract.

When this gap appears, propose a v2 judge pass that extracts the
structured findability signals the eval's predictors actually need.
This is cheap (10h at 3-judge parallelism for 24K records) compared to
running a downstream replication campaign on bad triage.

Cross-walk against any existing findability/cards repo before running v2.
The findability repo often already has the signal layer — you just need
to JOIN on the document ID.

## Pitfalls

- **Don't propose generic "random vs stratified vs hand-picked" options
  when an eval report exists.** That's the same anti-pattern as proposing
  diagnostics when the bug is already documented. Read the report first.
- **Don't trust the upstream judge's verdict in isolation.** If the eval
  found that "public code + public data" predicts success and the judge
  doesn't see those fields, the judge's REPLICABLE_NO_LAB is necessary
  but not sufficient. Always intersect with a structural-signals layer.
- **Domain mean-score tables are the easiest carve.** The eval's
  Domain × Mean-Score table tells you directly which subject codes punch
  above weight. Use them for the Tier-3 diversity fill — not for Tier 1
  (the per-paper signals are stronger).
- **The eval report's "Retry / upgrade pipeline" section names papers
  that previously scored low.** Don't include those in Tier 1 unless the
  upgrade was completed. Check `DEFERRED_PAPERS.md` or equivalent.
- **The eval report is often months old.** Confirm the failure-mode
  buckets are still current — sometimes proprietary access has been
  resolved (BV-BRC access granted, etc.) and the exclude rule should be
  loosened.

## Worked example: OSTI 24K → 1K (2026-06-06)

- Eval report: `rick-stevens-ai/replication-project/REPLICATION_EVALUATION_REPORT.tex`
  (60 papers, mean Coverage 7.48, mean Agreement 8.02)
- Upstream judge: Argo Sonnet 4.6 on title+abstract+subjects, 65,741
  papers, three-way verdict (REPLICABLE_NO_LAB / NEEDS_LAB / UNCLEAR)
- Cross-walk source: `osti-cards` repo, sample-200 findability results
  on data/model/agent cards (12% FOUND_DEPOSIT, 13.5% FOUND_RUNNABLE)
- Triage proposed: 3 tiers grounded in eval's "open-source code + public
  data" finding, hard excludes from eval's four failure modes
- v2-judge recommendation: re-judge REPLICABLE_NO_LAB pool with prompt
  augmented to extract findability signals → ~10h at 3-judge parallelism

Hand-off pattern: present the criteria as a proposal, not a fait accompli —
the user still owns the final tier-size and threshold calls.
