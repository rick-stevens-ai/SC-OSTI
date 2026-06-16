# LLM-driven taxonomy design + classifier trial (2026-06-09 v2)

Companion to `taxonomy-merge-from-community-sources-2026-06-09.md`. That
reference covers PULLING from upstream sources; this one covers the
**rework when Rick says "use LLM not regex" and "target 50-100 leaves
with ~30 supergroups"**, plus the **5K smoke-test trial** that should
gate any taxonomy before production use.

## When to use this reference

After producing a v1 merged taxonomy from community sources, the user often
wants a v2 that:

- Uses LLM extraction instead of regex (per the corpus-structured-extraction
  HARD RULE — see the "REGEX HARD RULE consolidated 2026-06-09" memory)
- Is **smaller** than the raw union (239 leaves → 81 leaves typical target)
- Has **explicit supergroup count** (~25-30) for the top-level browse
- Has been validated against a real sample of the target corpus

This reference covers the three pitfalls that bit during the v1 → v2 rework
plus the trial design that surfaces problems before scale.

## Pitfall 1: LLM hallucinates enumeration when asked for canonical lists

When you ask an LLM "what are all the canonical X codes in the Y taxonomy",
the model **synthesizes from training knowledge**, which:

- Mixes outdated codes with current ones
- Duplicates entries that have semantic overlap
- Invents codes that "should exist" by analogy

**Real failure case 2026-06-09**: asked llama70 for the canonical OSTI DOE
subject code list. Got back 65 codes including:

- `08 NUCLEAR DISARMAMENT`, `48 NUCLEAR DISARMAMENT`, `98 NUCLEAR DISARMAMENT`
  — same name, three different codes (all hallucinated except possibly 98)
- `51 MATERIALS SCIENCE` — duplicates the canonical `36 MATERIALS SCIENCE`
- `80` named with the same content as `21`
- `82 Catalysis` (title-case, not all-caps like the real codes)

**Resolution**: use **empirical sampling from the live corpus** as the
ground truth. Sample 5K OSTI records across multiple years, extract the
`subjects[]` field, pull out everything matching `^(\d{2})\s+(.+)$`. The
46 codes that appear in real records are authoritative. The LLM's 65 are
training-knowledge fantasy.

**Generalizes to**: any taxonomy where the source has machine-enforced
identifier codes (DOE numbered codes, ICD-10, SCImago categories, MeSH
trees). Don't ask the LLM to enumerate. Ask the LLM to interpret what
you found.

The general rule: **LLM for interpretation / merging / naming, empirical
sample for enumeration.** The HARD RULE about regex applies symmetrically
here — neither regex nor LLM-from-training-knowledge is the right
authority for "what are all the codes in system X." The corpus is.

## Pitfall 2: LLM defaults to uniform distribution when asked to "design a taxonomy"

Prompt: "design an 80-leaf taxonomy with about 25-30 supergroups."

First-attempt llama70 response: **18 supergroups × 3 leaves each = 54 leaves**.
Aesthetically tidy, completely useless. Particle physics gets 3 leaves;
condensed matter gets 3 leaves; environmental science gets 3 leaves. Real
scientific output is wildly uneven — particle physics + condensed matter +
materials science + astro between them dominate physical-sciences output,
while plasma fusion is one bucket and atomic-optical is one bucket.

**Fix**: rewrite the prompt with HARD REQUIREMENTS:

```
HARD REQUIREMENTS:
- EXACTLY 80 leaves total (not 75, not 85 — count and verify)
- UNEVEN distribution per supergroup — between 2 and 5 leaves
- ~25-30 supergroups
- Cover the FULL union of these source taxonomies: <list>
- Sample distribution from the target corpus: <top-20 categories>
- DOE-distinctive coverage required for: nuclear fuel cycle, fossil energy,
  reactor physics, plasma fusion, radiation protection, energy policy
```

Second-attempt llama70 response with these constraints: 81 leaves across
28 supergroups, distribution like particle-physics × 4, materials × 3,
fusion × 2, atomic-optical × 2. Uneven, corpus-shaped, usable.

**Generalizes to**: any "design X categories of Y" prompt where realistic
output is power-law-distributed. The LLM defaults to uniform unless you
override.

## Pitfall 3: Menu-prompt format induces position-index returns

When presenting a classification menu to an LLM:

```
WRONG:
[01] particle-physics-experiment   - high-energy collider physics, LHC, ATLAS
[02] condensed-matter-theory       - many-body theory, DMFT, tensor networks
...
[81] econ-policy                   - economic analysis, policy modeling

Output: leaf_id

RESULT: model returns "17" or "20" — the position number, not the leaf_id
```

The `[NN]` prefix is irresistible to instruction-tuned models. They see a
numbered list and assume "pick a number." Even when the prompt says "output
the leaf_id string," ~40% of responses come back as `"17"`.

**Fix**: drop the numeric prefix and explicitly disclaim numbers:

```
RIGHT:
particle-physics-experiment   - high-energy collider physics, LHC, ATLAS
condensed-matter-theory       - many-body theory, DMFT, tensor networks
...
econ-policy                   - economic analysis, policy modeling

Output: a single leaf_id string from the menu above.
DO NOT output position numbers or indices.
DO NOT output "17" or "[17]" — those are not leaf IDs.
DO output strings like "particle-physics-experiment" or "econ-policy".
```

After this change, position-index returns dropped from ~40% to 0/30 in
the next smoke-test batch.

**Generalizes to**: any classification / multiple-choice / select-from-list
LLM prompt. Never number the options unless the numbers ARE the expected
output.

## 5K classifier trial design

Before declaring any new taxonomy ready for production, run it against
~5K random papers from the target corpus and look for:

1. **Per-leaf distribution** — leaves with 0 hits are dead categories that
   shouldn't be in the taxonomy. Leaves with >30% of all hits are
   over-broad and should be split.
2. **Confidence histogram** — fraction of high/medium/low confidence
   classifications. Low-confidence > 40% means the menu is missing
   categories the corpus needs.
3. **Hallucinated leaves** — any returned `primary_id` that's not in the
   menu. Each hallucination is a signal that the menu is missing a real
   category (e.g. `structural-biology` showed up in the OSTI smoke
   indicating that bucket may belong in the taxonomy).
4. **No-abstract drop rate** — what fraction of randomly-sampled papers
   have a non-trivial abstract. OSTI is ~40% no-abstract. Over-sample by
   1/(1 - drop_rate) to hit the target classified-count.
5. **Supergroup balance** — sum hits by supergroup. A supergroup with 0
   hits across 5K papers means either the supergroup doesn't belong in
   the taxonomy or the corpus genuinely doesn't cover it.

### Trial script shape

`taxonomy/trial_5k.py` template:

```python
# Inputs: merged_taxonomy_v2.json (LEAF_BY_ID dict), N_SAMPLE
# 1. SELECT random N*1.3 osti_ids from phase1_master.db (over-sample for
#    no-abstract drops)
# 2. ThreadPoolExecutor(16) → for each:
#    a. JIT fetch abstract from OSTI /api/v1/records/{id}
#    b. Skip if description < 100 chars OR "No abstract prepared"
#    c. Build menu prompt (NO position numbers per pitfall 3)
#    d. POST to llama70 endpoint
#    e. Parse JSON: {primary_id, confidence, secondary_id}
#    f. Validate primary_id ∈ LEAF_BY_ID (else log to hallucinated)
# 3. Emit: trial_5k_classifications.tsv + trial_5k_errors.tsv +
#    trial_5k_hallucinations.tsv
```

Critical bits:

- **JIT abstract fetch** — OSTI's `phase1_master.db` has no description
  column. Don't try to use cached text; fetch live.
- **No-abstract handling** — sub-100-char descriptions are noise. Drop
  them rather than asking the LLM to classify "Title only" papers.
- **Validation against LEAF_BY_ID set** — any `primary_id` not in the menu
  goes to a separate `hallucinations.tsv` for analysis. This is the most
  useful single signal for "what's missing from your taxonomy."
- **Confidence as JSON field** — ask the model for `"confidence":
  "high"|"medium"|"low"` and use the distribution as a quality probe.

### What "good" looks like

For a 5K trial against an 81-leaf taxonomy:

- ≥95% non-hallucinated returns (≤5% primary_id not in menu)
- High-confidence fraction ≥40%, low-confidence ≤20%
- All 28 supergroups have ≥10 hits
- ≥75 of 81 leaves have ≥5 hits (allow a handful of edge-case leaves
  with low counts — those are fine for completeness)
- No single leaf >15% of all hits (a leaf with 25% saturation is
  swallowing classifications that belong elsewhere)

### What "bad" looks like (and what to do)

| Signal | Likely cause | Fix |
|---|---|---|
| 1+ supergroup with 0 hits | Taxonomy includes categories the corpus doesn't cover | Drop the supergroup OR find the missing-from-taxonomy category that's eating its share |
| 1 leaf with 25%+ hits | Leaf is over-broad | Split into 2-3 finer leaves OR check if a sibling leaf is missing |
| Hallucinated `structural-biology` 5+ times | Taxonomy is missing structural-biology as a leaf | Add the hallucinated category to the taxonomy if it appears repeatedly |
| Low-confidence > 40% | Menu items are too similar or descriptions are too thin | Rewrite leaf descriptions with clearer disambiguators |
| No-abstract rate > 50% | Sampling is biased toward old / metadata-only records | Filter the source DB by year or by `has_abstract` before sampling |

## Cost / wall

- 5K classifications, llama70 (CELS free endpoint), 16 workers, JIT
  abstract fetch: ~30-45 minutes wall. Free.
- OSTI API calls: 5K × 1.3 oversample = 6.5K calls. OSTI tolerates 4-8
  workers cleanly; 16 workers may trip the API rate signature. Watch for
  `RemoteDisconnected` in errors and back off to 8 if it does.
- If using Argo Sonnet 4.6 instead: ~1-2s per call → ~30 min at 16 workers,
  ~$25-50 depending on prompt size.

## Linked artifacts

- The trial script template lives in `~/code/osti-replication-candidates/taxonomy/trial_5k.py`
  for the OSTI/llama70 specific case. Generalize by:
    - Swap `phase1_master.db` query for your corpus ID source
    - Swap OSTI abstract-fetch URL for your metadata API
    - Swap LEAF_BY_ID source for your taxonomy JSON
- The CELS endpoint shape is in `references/argo-endpoint.md` (sibling).
- The "no regex when LLM works better" rule that drove this rework: see
  the HARD RULE section in SKILL.md and the "REGEX HARD RULE" memory.
