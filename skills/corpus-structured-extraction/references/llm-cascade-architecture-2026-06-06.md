# LLM cascade architecture for large-corpus judgment

When you need to judge 50K+ documents and a single Argo Sonnet pass would cost
hours/dollars/quota, the cascade pattern keeps judgment entirely in the LLM
tier while concentrating expensive calls only on the candidates that matter.

This is the **correct** alternative to the regex-pre-filter anti-pattern (see
SKILL.md HARD RULE + REINFORCEMENT). Regex feeds judgment selection → forbidden.
Cheap LLM feeds judgment selection → fine, because the cheap model is doing
the judgment, just at lower fidelity.

## When to use

- Corpus is large enough that single-pass Sonnet is uncomfortable (>20K docs)
- You need to triage down to a top-N selection (1K finalists from 25K candidates)
- You can afford ~3-6 cheap-model req/s for hours but not Sonnet-Opus at the
  same rate
- The judgment has a natural "obvious no" tier (HPC-only, proprietary, etc.)
  that a cheap model can filter cleanly

If the corpus is <5K or you need fine-grained scoring on everything, just run
Sonnet on all of it.

## The three-stage cascade

```
Stage 1: cheap+fast triage         → runs on ALL docs (e.g. 65,741)
   model: llama70 (CELS, free, ~4-6 req/s)
   input: title + abstract + first 4K chars of fulltext
   output: binary flags + verdict + 1-line why
   purpose: kill obvious no's and identify candidates worth deeper look
   ↓ filter: keep docs where (replicable=YES) AND (not HPC-only) AND (not proprietary)
   ↓ typically ~10-20% of input → ~5-15K candidates

Stage 2: capable extraction         → runs on Stage-1 hits only
   model: argo:claude-sonnet-4.6
   input: full text (chunked if >100K chars)
   output: structured fields with paper-text quotes:
     - where code lives (named URLs with line refs)
     - where data lives (named registries + accessions)
     - compute scale estimate with quote
     - hyperparameter risk with method-specific reasoning
     - replicability_score 1-10 with rationale
   purpose: high-fidelity signal for candidates that actually matter
   ↓ keep top ~2-3K by score for finals

Stage 3: multi-judge consensus       → runs on Stage-2 top hits
   models: sonnet 4.6 + llama70 + gemma4 (or oss120 for harder tasks)
   input: Stage-2 structured signals
   output: independent score per judge, majority verdict
   purpose: methodology requirement (kill single-judge-at-scale bias)
   ↓ produces final ranked pool of ~1K selections
```

## Cost shape

For the OSTI 65,741-paper triage:
- Single Sonnet pass:  65,741 × 1 Sonnet call = 65,741 expensive calls
- Cascade (this pattern):
  - Stage 1: 65,741 × 1 llama70 call    = 65,741 free local calls (~3-4h)
  - Stage 2: ~8,000 × 1 Sonnet call     = 8,000 expensive calls (~1h)
  - Stage 3: ~2,500 × 3 judge calls     = 7,500 mixed calls (~30min)
  - Total expensive calls: 8,000  (8× reduction)

## Stage 1 throughput data (CELS llama70, M1 mini, 2026-06-06)

Measured on the OSTI 65,741-paper triage:

- **Endpoint capacity probe** (tiny prompt, 20 calls, varying concurrency):
  - concurrency=1:  1.6 req/s   (sequential, network-bound)
  - concurrency=5:  18.7 req/s  ← endpoint sweet spot for short prompts
  - concurrency=10: 17.1 req/s
  - concurrency=20: 4.4 req/s   (server-side queuing degrades, calls go to 4.5s each)
  Quick way to find a new endpoint's ceiling: run this probe before scaling.
- **Production throughput** (12K-char fulltext prompts, 16 workers):
  - **1.78 req/s sustained**, median per-call latency **8.4s**
  - 65,741 papers → ~10.5h wall clock
  - Throughput is **generation-bound on output tokens**, not network-bound.
    Adding workers beyond ~16 doesn't help because each call generates ~50
    output tokens which dominates the round trip.
- **Smoke vs production gap**: 5-paper smoke at 5 workers showed 1.28 req/s
  / 3.45s median; production at 16 workers showed 1.78 req/s / 8.4s median.
  The slowdown is because production inputs hit the full 12KB cap; smoke
  inputs included some short-fulltext papers that completed fast. **Don't
  trust smoke throughput for capacity planning** — run a 200-call probe with
  realistic inputs before quoting ETA.

### Pitfall: input-size vs signal trade is real, test it

For the Stage 1 fulltext cap, smaller is faster but loses signal on close-call
papers. Empirical test (5 known REPLICABLE_NO_LAB genome papers, 2026-06-06):

| Cap   | Latency | Signal degradation |
|-------|---------|---------------------|
| 8KB   | ~3.3s   | Couldn't distinguish "paper ran sequencing" from "paper analyzed deposited sequences" for whole-genome studies. 1/5 verdict flip, 1/5 lost REPLICABLE_LIKELY flag |
| 12KB  | ~3.4s   | Kept the discrimination, latency identical (output-bound, not input-bound) |

**Lesson**: when the model is output-token-bound (short structured response),
larger input is essentially free. Only trim input when it actually shortens
wall time. Always test the signal trade on close-call cases before committing
a cap — the verdict flips are not random, they cluster on exactly the borderline
papers you most need to get right.

## Stage 1 prompt shape (cheap-model triage)

The triage prompt must be **short, structured, and ask only for binary flags**.
Cheap models drift on long prompts; keep the rubric tight. Output strict JSON.

```
You are screening papers for a replication study. Return STRICT JSON only.

Schema:
{
  "verdict": "REPLICABLE_NO_LAB" | "NEEDS_LAB" | "UNCLEAR",
  "mentions_code_release": true | false,
  "mentions_public_dataset": true | false,
  "compute_scale": "WORKSTATION" | "SINGLE_GPU" | "MULTI_GPU" | "HPC" | "UNCLEAR",
  "proprietary_pipeline": true | false,
  "why": "<one short sentence>"
}

Definitions:
  REPLICABLE_NO_LAB: theory/numerical-sim/public-data analysis/ML/stats-reanalysis only
  NEEDS_LAB: requires new wet-lab/beamline/instrumentation/field samples
  UNCLEAR: too brief to decide
  mentions_code_release: paper text references code/GitHub/source release/named package
  mentions_public_dataset: paper text references deposited dataset/accession/public benchmark
  compute_scale: WORKSTATION (single machine), SINGLE_GPU (one GPU node),
                 MULTI_GPU (2-8 GPUs), HPC (10^4+ GPU-hours), UNCLEAR (no hint)
  proprietary_pipeline: requires gated vendor software (BV-BRC, SEEDtk, etc.)

Output STRICT JSON only. No markdown fences, no prose.
```

## Stage 2 prompt shape (capable extraction)

The Stage 2 prompt is structured-output extraction with **paper-text quotes
required**. The quotes prevent the model from hallucinating signals; if the
quote can't be found in the source, the field is wrong.

```
You are reading a scientific paper to extract replication-readiness signals.
For each field, include a 1-2 sentence verbatim quote from the paper that
supports your answer. If no supporting quote exists, set the field to UNKNOWN.

Schema:
{
  "code_locations": [
    {"url": "<URL>", "quote": "<verbatim paper text>", "kind": "github|gitlab|hf|zenodo|other"}
  ],
  "data_locations": [
    {"registry": "<name>", "accession": "<ID>", "quote": "<verbatim text>"}
  ],
  "compute_scale_estimate": {
    "value": "WORKSTATION|SINGLE_GPU|MULTI_GPU|HPC|UNCLEAR",
    "quote": "<verbatim text or empty>"
  },
  "hyperparameter_risk": {
    "value": "LOW|MEDIUM|HIGH",
    "reasoning": "<method-specific reason, 1 sentence>"
  },
  "replicability_score": <int 1-10>,
  "score_rationale": "<2-3 sentence summary citing the signals above>"
}
```

## Stage 3: multi-judge consensus

Same prompt as Stage 2, run independently on three judges. Aggregate:
- If all 3 judges' scores within 2 of each other → use mean
- If one judge disagrees by >3 → flag for human review, use median
- Verdict: majority of (REPLICABLE/NEEDS_LAB/UNCLEAR) calls

See `scripts/judge_bakeoff.py` for the multi-judge fan-out template.

## Pitfalls specific to cascading

- **Don't let Stage 1's binary flags become inputs to Stage 2's prompt.** The
  Stage 2 model should see the paper text, not Stage 1's summary — Stage 1 was
  triage, its conclusions are not signal. Pass only the document, not the
  Stage 1 output, to Stage 2. (If Stage 1 was wrong about "no code mentioned",
  showing that to Stage 2 will bias it.)
- **Stage 1 false negatives are unrecoverable.** Anything Stage 1 filters out
  is dropped from the pipeline. Be conservative with the filter — when in
  doubt, let it through to Stage 2. The cost of Stage 2 on an extra 2K papers
  is 10 minutes; the cost of dropping a high-value paper is the entire study
  missing it.
- **llama70 / gemma4 are fine for binary classification but bad at structured
  scoring.** They drift on 1-10 scales. Use them for Stage 1 (where they shine)
  and Stage 3 majority votes (where the structure comes from aggregation
  across judges), but NOT for Stage 2 (where you need consistent 1-10
  granularity per paper).
- **Reasoning models (kimi-k2.6, oss120) are still wrong for cheap triage.**
  Same trap as documented in SKILL.md: they abstain too aggressively, return
  UNCLEAR on 50%+ of calls. Stage 1 = instruction-tuned non-reasoning only.
- **Smoke-test EACH stage independently before chaining.** Stage 1 smoke (50
  papers), then Stage 2 smoke on the 5-10 hits from Stage 1 smoke, then
  Stage 3 smoke on top 3. Catch prompt issues at each level before the cost
  compounds.

## Anti-pattern: the regex-pre-filter shortcut

The seductive wrong path: "I'll just regex for GitHub URLs and 'Code
Availability' sections to pre-filter, then LLM-judge the hits." This is the
exact failure mode that prompted the REINFORCEMENT section in SKILL.md.
Cheap-LLM triage costs ~$0 and ~3 hours on a free CELS endpoint; that is the
right answer, not regex. If you're tempted to regex because "the LLM is too
slow", you're optimizing the wrong axis.

## Reference implementation status (2026-06-06)

A scaffolded cascade for the OSTI 65,741 task lives at
`~/code/osti-replication-candidates/` with these expected stages:
- `llm_judge_cascade_stage1.py` (cheap llama70 triage)
- `llm_judge_v2.py` exists but takes single Sonnet pass per paper — being
  retired in favor of cascade
- `extract_fulltext.py` (pymupdf → fulltext/<id>.txt) — preprocessing only,
  no judgment
- `recover_missing_fulltext.py` (OSTI PURL re-download for empty PDFs) —
  preprocessing only, no judgment

The cascade pattern itself is general and reusable for any large-corpus
judgment task.
