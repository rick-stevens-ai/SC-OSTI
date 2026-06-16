# Pipeline coverage gap as selection bottleneck (OSTI 1K, 2026-06-15)

When the deliverable is "select N best documents from a corpus" and the
selection logic is a crosswalk / join across multiple upstream pipelines, the
real bottleneck is often **not** the selection rubric being too strict — it's
that one of the upstream pipelines only ran on a development sample.

## The failure case

**Task**: select ~1,000 OSTI papers from the REPLICABLE_NO_LAB pool (24,388
papers) most likely to be actually replicable.

**Architecture**:
- Stage A: LLM judgment pipeline → `all_verdicts.jsonl` (65,741 paper-verdicts)
  → filtered to `replicable_pool.jsonl` (24,388 REPLICABLE_NO_LAB)
- Stage B: xCard findability pipeline → resolves DOIs/registry/GitHub for each
  card → produces FOUND_DEPOSIT / FOUND_RUNNABLE / FOUND_DOI_ONLY / etc.
- Stage C: `crosswalk.py` joins A ∩ B → tier1 (REPLICABLE ∩ FOUND_DEPOSIT|
  FOUND_RUNNABLE), tier2 (REPLICABLE ∩ FOUND_DOI_ONLY|FOUND_PAPER_ONLY)

**Observed**: crosswalk produced tier1=12, tier2=43. Wildly short of the 1,000
target.

**Wrong instinct**: "the consensus rubric is too strict / the LLM-judged
REPLICABLE_NO_LAB pool is too narrow / we need to relax the findability
gates."

**Real cause**: Stage B (findability) had only run on the **development
samples** — 200 data cards + 200 model cards + 86 agent cards = **486 of the
5,945 total cards**, or 8% coverage. The hard-coded sample limitation was in
a docstring comment in `crosswalk.py`:

```python
# NOTE: lower-bound — findability only ran on sample-486
# tier counts will scale ~12x once full corpus is processed
```

The Stage-A LLM judgment had run on all 65,741 records. Stage-B findability
was the development-time bottleneck and never got rescaled.

## The fix (30 minutes of work, not methodology change)

1. **Find the upstream coverage gap** — `wc -l` the findability result files
   vs source corpus size:

   ```bash
   wc -l samples/sample200_results.jsonl samples/sample200_models_results.jsonl samples/all86_agents_results.jsonl
   # 200 + 200 + 86 = 486
   ls ~/Dropbox/ARGONNE-PAPERS/XCARDS/MARKDOWN-{DATA,MODEL,AGENT}-CARDS/ | wc -l
   # 4628 + 1231 + 86 = 5945 → 8% coverage
   ```

2. **Patch the pipeline glob if input format expanded** — the existing
   pipeline globbed `*.txt`, but the new clean-frontmatter cards are `.md`:

   ```python
   files = sorted(in_dir.glob("*.txt")) + sorted(in_dir.glob("*.md"))
   ```

3. **Smoke-test the format change** — 20 .md cards: 100% FOUND_DOI_ONLY (vs
   51% on .txt sample). YAML frontmatter `doi:` field is dramatically more
   findable than `[Not specified]` placeholders.

   **BUT — format-direction asymmetry by signal-class** (verified 2026-06-15,
   second-pass on full corpus): the .md curation is BETTER for DOI extraction
   (clean frontmatter `doi:` field) but WORSE for runnable-code detection
   (GitHub/HF Hub URLs stripped during curation). Running findability against
   `MARKDOWN-MODEL-CARDS/` (1231 cards): 0 FOUND_RUNNABLE / 0 FOUND_PAPER_ONLY
   / 31 FOUND_DEPOSIT / 734 FOUND_DOI_ONLY. Re-running against the parallel
   `NONEMPTY-MODEL-CARDS/` (same 1231 papers, raw extract-o-matic .txt
   format): 27 FOUND_RUNNABLE / 28 FOUND_PAPER_ONLY in the sample baseline.
   Same OSTI IDs, different format → different signal density per verdict
   class. **Rule: format choice is signal-class-dependent. If you have
   parallel format variants of the same corpus (raw vs curated, original vs
   distilled), pick per signal class — possibly run BOTH and merge per-card
   per-signal**:

   ```python
   # Pseudocode: best-of-N merge across format variants
   for card_id in universe:
       md_result = findability(md_dir / f"{card_id}.md")    # strong on DOI
       txt_result = findability(txt_dir / f"{card_id}.txt")  # strong on URL
       merged = {
         "doi_resolves": md_result.doi_resolves or txt_result.doi_resolves,
         "github_url": txt_result.github_url or md_result.github_url,
         "hf_url": txt_result.hf_url or md_result.hf_url,
         ...
       }
   ```

   Curation pipelines that produce a "clean" version often strip exactly the
   noisy free-text where the runnable-artifact URLs live. Check the curator
   source (here: `extract-o-matic.py` → `augment_cards.py` → markdown emit)
   to confirm what's being dropped before running findability against the
   curated version alone.

4. **Launch full-corpus runs in background**:

   ```bash
   nohup python3 src/pipeline.py ~/Dropbox/ARGONNE-PAPERS/XCARDS/MARKDOWN-DATA-CARDS \
     samples/full_corpus_results/data_full_results.jsonl 8 \
     > samples/full_corpus_results/data_full.log 2>&1 < /dev/null &
   ```

   Throughput on M1 at 8 workers × 4 cards × 8 signals = ~3 cards/s, so 4,628
   cards = ~25 min wall.

5. **Write `crosswalk_v2.py`** — same join logic as v1, reads from the new
   `full_corpus_results/` dir instead of the sample files.

6. **Wait, run crosswalk_v2, assess** — expected ~120 tier1 + ~430 tier2
   (12x sample baseline since 5945/486 = 12.2x denominator).

## Sibling pitfall — wrong pipeline-per-card-kind (verified 2026-06-15 same session)

Same project, second self-inflicted wound: the xcard project has **two
different findability pipeline scripts** with overlapping interfaces:

| Pipeline | What it extracts | Right for |
|----------|------------------|-----------|
| `pipeline.py` | DOI + GEO/SRA/PRIDE/Zenodo/Figshare/PDB accessions, DataCite type → FOUND_DEPOSIT | **DATA cards only** |
| `model_agent_pipeline.py` | adds GH/GL/HF/arXiv extractors + `code_validators` → FOUND_RUNNABLE | **MODEL + AGENT cards** |

After fixing the coverage gap (Step 1-3 above) I re-ran `pipeline.py` against
all three card kinds. Data results looked plausible. Model+agent results came
back with **zero FOUND_RUNNABLE** — but ~30% of the source cards literally
contain `github.com` URLs in raw grep. The verdict counter was structurally
blind to code repos because the GH/HF/arXiv regexes aren't in `pipeline.py`.

The hint was hiding in the existing artifacts: `sample200_results.jsonl` (data)
was made with `pipeline.py`, but `sample200_models_results.jsonl` and
`all86_agents_results.jsonl` were both made with `model_agent_pipeline.py`.
Filename pattern tells you which pipeline produced the existing baseline.

### The rule, generalized

When a project has multiple sibling extraction pipelines that share inputs and
output schema (or near-schema):

1. **Look at the existing baseline JSONLs** before launching a new run.
   `head -1 <sample>.jsonl` and compare the set of keys to the pipeline you're
   about to invoke. If keys differ, the baseline was made by a different
   script. Find that script.
2. **Grep the candidate pipelines for the regex/validator family you need**:
   `grep -lE "GH_RX|github.com|HfApi|huggingface" src/*pipeline*.py`. The
   pipeline that mentions the signal class you care about is the right one.
3. **Match pipeline to card kind by convention, not by name similarity**.
   "pipeline.py" and "model_agent_pipeline.py" sound interchangeable; they
   are not.

## Sibling pitfall #2 — multiple parallel corpus variants (verified 2026-06-15)

Same xcard project also has **three parallel corpus shapes** for the same
underlying 5,945 papers:

| Dir | Format | Content | Right for |
|-----|--------|---------|-----------|
| `XCARDS/MARKDOWN-{KIND}-CARDS/` | `.md` | curated YAML frontmatter, narrative body, **GitHub URLs stripped** | DOI-only findability, presentation |
| `GOOD/NONEMPTY-{KIND}-CARDS/` | `.txt` | extract-o-matic raw output, **all signals intact** | findability needing GH/HF/arXiv |
| `GOOD/ALL-PAPERS-{KIND}-CARDS/` | `.txt` | 18,902 each — mostly `[Not specified]` placeholders for papers without strong signals | denominator-counting only, not findability |

I burned 10 minutes running findability against MARKDOWN-{MODEL,AGENT} cards
(curated, stripped GitHub) → 0 FOUND_RUNNABLE → diagnosed as "wrong pipeline"
when the corpus was *also* wrong. The right combination for model/agent
runnability is `model_agent_pipeline.py` × `NONEMPTY-{KIND}-CARDS/`.

### The rule

When the user mentions "the X cards" / "the corpus" / "the papers" and
multiple shapes exist on disk:

1. `find <project>/ -type d -name '*CARDS*' -maxdepth 4` to enumerate variants.
2. For each variant, `ls <dir> | head -3 && wc -l <dir>/<first_file>` —
   format + size tells you which is curated-distilled vs raw-extract vs
   placeholder-filled.
3. `grep -c 'github.com' <dir>/*.{md,txt} 2>/dev/null | awk -F: '{s+=$2} END {print s}'`
   for any signal class you care about. The dir with the highest count of the
   signal is the right one for that signal's findability pass.
4. The curated/distilled variant is the wrong corpus for code-repo
   findability EVEN IF it's what gets shown to humans by default.

## The rule (selection-pipeline debugging)

When a selection / crosswalk / join pipeline produces fewer hits than expected:

**Step 1**: don't tune the rubric. `wc -l` every upstream output file and
compare to source-universe size. If any upstream is at <50% coverage, that's
your real bottleneck.

**Step 2**: grep the join script for "sample" / "limit" / "lower bound" /
"subset" / "TODO" / "FIXME" in comments and variable names. Sample-time
limitations are usually self-documented but easy to miss when you start
debugging from the join script.

**Step 3**: scale the upstream pipeline before touching join logic. Often
cheaper than methodology debate.

**Step 4 (new)**: if scaled-upstream still produces zero hits in a verdict
class you expected to be populated, the script may be structurally blind to
that signal class — **not** the cards being empty. Grep both the pipeline
script (for the regex/validator) AND the source cards (for the literal
signal). If signal exists in cards but verdict is zero, you have the wrong
pipeline. Find the sibling script that handles that signal class. See
"Sibling pitfall — wrong pipeline-per-card-kind" above.

**Step 5 (new)**: if pipeline + signal both check out and verdict is still
empty, you may have the wrong corpus variant. See "Sibling pitfall #2 —
multiple parallel corpus variants" above.

**Step 6 (new, the meta-bottleneck)**: after Steps 1-5 are clean and the
crosswalk numbers stabilize, compute the **intersection rate**:
`|upstream_cards ∩ selection_universe| / |selection_universe|`. If this is
<10%, no amount of upstream-pipeline tuning will hit the selection target —
the carded subset is structurally too narrow vs the selection pool. You have
two real options:

1. **LLM judgment on the un-carded selection-pool members directly** —
   bypass the card-findability path entirely. Pull whatever metadata/text
   IS available for those records (abstract, OSTI subject codes, fulltext
   if on disk) and run a parallel judgment cascade. Same Stage-3 prompt,
   different input source. This is the fast path when the deliverable is
   time-sensitive.
2. **Extend the upstream card pipeline to cover the gap** — generate cards
   for the un-carded selection-pool members, re-run findability, re-run
   crosswalk. Durable but slow (cards@~1-3/s × tens of thousands).

Worked numbers from the OSTI 1K task (2026-06-15 evening):
- Card universe: 5,945 papers (xCards exist)
- Selection universe: 24,388 papers (REPLICABLE_NO_LAB pool from LLM judgment)
- Intersection: 726 papers (**3%** of selection-universe)
- Even with full-coverage findability + correct pipeline + correct corpus:
  v2 produced 55 tier1 + 428 tier2 = 483 (vs 1,000 target)
- Pre-fix v1: 12 tier1 + 43 tier2 = 55. So 8.8x growth as expected, but
  still short because the intersection ceiling is the real wall.
- Fix path: ship Stage-3 LLM judgment on the un-carded 23,662 to fill the
  ~520-paper gap. Cost: 23k Sonnet calls ≈ 1-2h on the standard ALCF
  endpoint, free.

**Diagnostic pattern**: when crosswalk-v2 is still short of target even
after Steps 1-5, surface the intersection rate explicitly to the user
before proposing fixes. The intersection-rate framing is what unlocks
the "scale upstream cards" vs "bypass cards for direct LLM judgment"
decision — without it the conversation tends to circle back to tuning
rubrics that aren't the real constraint.

## Stage-3 worked recipe — un-carded LLM judgment fill (2026-06-15 evening)

After v2 crosswalk produced 483 tier1+tier2 against a 1,000 target with the
intersection rate at 3%, the fast path is Stage-3 LLM judgment on the
un-carded 23,662 REPLICABLE_NO_LAB papers using only the metadata + the
prior reviewer's reasoning already in `replicable_pool.jsonl`.

### Prompt design

The Stage-2 judgment that produced REPLICABLE_NO_LAB labeled the paper as
"appears reproducible without lab equipment." Stage-3 is a stricter second
pass with an explicit credibility scale:

```
HIGH (0.7-1.0): widely-available methods/code, standard ML benchmark,
                well-known simulation tool with public input files,
                theory paper whose calculations any competent reader
                could redo
MEDIUM (0.4-0.7): plausibly replicable but key dependencies likely closed
                  (custom code not released, proprietary input data)
LOW (0.0-0.4): despite prior label, reproduction would require non-public
               artifacts (collaboration-internal data/code, custom pipeline,
               fit to private data)
```

Force STRICT JSON output: `{stage3_verdict, stage3_score, stage3_why,
needs_code, needs_data, domain_tag}`. Temperature 0, max_tokens 400.

### Calibration smoke (50 papers, ~50s wall, $0 on Argo)

Distribution that came back:
- HIGH: 12% (theory papers — analytical derivations, anyone can redo)
- MEDIUM: 60% (the bulk — plausible but unverifiable from metadata)
- LOW: 28% (correctly catching DES weak-lensing, CMS searches, lattice QCD
  that need collaboration-internal artifacts)

Calibration looked right because:
- HIGH samples were uniformly theory papers with specific, citable reasons
  ("analytical derivations using BDS ansatz", "standard perturbation theory")
- LOW samples named specific blocking artifacts ("DES SV imaging data + custom
  im3shape/ngmix pipelines", "CMS proprietary simulation framework")
- Sonnet's reasoning was specific and ungeneric — not pattern-matching from
  keywords

If the smoke shows >50% MEDIUM AND the MEDIUM reasoning is uniformly
vague ("plausibly replicable, depends on specific implementation"), the
prompt is too coarse — add explicit anchors for the HIGH bucket. If LOW
is <5%, the prompt is too generous (Stage-2 already flagged the obvious
cases).

### Resumable launcher pattern

```python
# stage3_judge.py — key shape, full template in
# scripts/stage3_uncarded_llm_judgment.py
def main():
    # 1. Build carded set (exclusion list)
    carded = set()
    for fname in ("data_*.jsonl", "model_*.jsonl", "agent_*.jsonl"):
        for p in glob(fname):
            for line in open(p):
                r = json.loads(line)
                if r.get("card_id"):
                    carded.add(str(r["card_id"]))

    # 2. Resume: skip already-judged
    done = set()
    if args.skip_existing and Path(args.output).exists():
        for line in open(args.output):
            done.add(str(json.loads(line)["osti_id"]))

    # 3. Queue = selection_pool - carded - done
    pool = [json.loads(l) for l in open(args.input)]
    queue = [p for p in pool
             if str(p["osti_id"]) not in carded
             and str(p["osti_id"]) not in done]

    # 4. ThreadPoolExecutor at 16 workers (Argo handles it easily)
    #    write append-only after each completion (resumable)
```

Throughput on Argo Sonnet 4.6 at 16 workers: **3.5 req/s sustained, zero
errors** on the 23,662-paper run (smoke was 1.03 req/s at 8 workers, scaled
~linearly to 16). Wall time for 23,662 papers: ~110 minutes.

### Cost vs alternative

| Path | Wall time | LLM calls | Cost | Recovery |
|------|-----------|-----------|------|----------|
| Extend card pipeline to 23,662 un-carded | ~4-8h cards + ~30min findability + crosswalk | mostly free local | $0 | full structural-signal validation |
| Stage-3 LLM judgment direct on un-carded | ~110min | 23,662 Sonnet | $0 on Argo | metadata-only judgment, no structural signals |

The two are complementary, not alternatives. Stage-3 first (fast, fills the
gap NOW for the deliverable), card-extension second (durable, gives the
project a higher-quality denominator for the next selection question).

### Projection from smoke

12% HIGH × 23,662 = ~2,839 projected HIGH papers from the un-carded pool.
Combined with v2 tier1 (55) = ~2,894 HIGH-credibility candidates. **Well
over the 1,000 target with high-quality picks** — final selection becomes a
ranking/diversification problem (across years, DOE labs, scientific domains)
rather than a coverage problem.

### Final-selection ranking pattern

Once Stage-3 completes, rank with:
1. tier1_v2 (FOUND_DEPOSIT/RUNNABLE structural signals) FIRST — highest-trust
2. Stage-3 HIGH (score >= 0.7) NEXT — high credibility from metadata
3. tier2_v2 (FOUND_DOI_ONLY/PAPER_ONLY) NEXT — partial structural signals
4. Stage-3 MEDIUM (score >= 0.55) FALLBACK if still short

Diversify across (year, lab, domain_tag) within each tier — don't let HEP-PH
theory dominate the top-100 because Sonnet finds theory papers easy to score
HIGH. Stratified sampling within tier preserves the corpus's domain breadth.

### When this recipe applies

- Selection target on a corpus where the carded subset is <10% of the
  selection universe (the intersection-rate gate from Step 6 above)
- Each un-carded record has at least title + subjects + prior LLM reasoning
  (the absolute minimum signal for metadata-only judgment)
- Time-to-deliverable matters more than provenance uniformity (mixing
  card-grounded tier1/2 with metadata-only Stage-3 means downstream consumers
  need to know which judgment provenance each pick has — store
  `pick_provenance: "card-tier1" | "card-tier2" | "stage3-high" | "stage3-medium"`
  in the output JSONL so the seam stays visible)

## Generalizes to

- Any multi-stage pipeline where Stage N+1 joins Stage N's output
- Recall@K-style benchmarks where the retrieval stage was sample-evaluated
- Coverage-then-recovery cascades (recon → fetch → recover) — easy to ship a
  recovery stage that only saw the smoke-batch's failure distribution
- LLM-pipeline cascades where a downstream judge filters a triage output —
  triage may have only run on a subset

## Pre-flight one-liner to add to any selection-pipeline driver

```bash
# Pre-flight: confirm upstream pipelines covered the full input universe
for f in upstream_a/*.jsonl upstream_b/*.jsonl; do
  echo "$f: $(wc -l < $f)"
done
echo "source universe: $(find /path/to/source -name '*.md' | wc -l)"
# If upstream counts are << source count, scale upstream FIRST
```
