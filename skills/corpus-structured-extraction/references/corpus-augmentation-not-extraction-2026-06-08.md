# Corpus augmentation pattern (vs fresh extraction)

When the user says "augment these N existing cards/reports with field X" —
distinct from "extract field X from this corpus" — the right shape is a
**two-pass cheap→LLM cascade with idempotent injection**, not a single
LLM pass.

Worked example: 2026-06-08, "augment 5,945 xCards with author names + emails
from the associated papers so we can track down missing items."

## When this pattern applies

- User has an existing structured artifact set (xCards, MD reports, JSON
  records, whatever) that's already been authored / extracted once.
- User wants to *add* a field or *upgrade* an existing field without
  destroying the rest of the document.
- A cheap deterministic source already covers some fraction of the
  augmentation (a SQLite from a prior extraction, an external metadata API,
  a sibling pipeline's output JSONL).
- The remaining gap is small enough that LLM-on-misses is affordable.

Contrast with fresh extraction (the rest of this umbrella skill): there's
no prior artifact to preserve, and you're producing the table from raw
documents end-to-end.

## The two-pass shape

### Pass 1 — Cheap deterministic source, canonical injection

For every target document:

1. Extract a stable key from the filename or frontmatter (osti_id, doi,
   arxiv id).
2. Query the cheap source (SQLite / API / JSONL) for known augmentation
   data.
3. Render a canonical Markdown block delimited by marker comments:

   ```
   <!-- AUGMENT:CONTACTS START -->
   ## Contacts (augmented)
   _osti_id: `1196234` · year: 2018 · doi: `10.1021/...` · source: contacts.db · as_of: 2026-06-08_

   | Role | Name | Email | Lab | Papers |
   |------|------|-------|-----|--------|
   | **corresponding** | Zhengchun Liu | `zhengchun.liu@anl.gov` | ANL | 8 |
   <!-- AUGMENT:CONTACTS END -->
   ```

4. **Upsert** the block: if markers exist in the document, replace the
   span between them; otherwise append at EOF.
5. One-time `.bak` per file the first time it's touched (skip backup if
   `.bak` exists — second-pass re-runs shouldn't overwrite the original).

Re-runs are idempotent: the marker regex finds the prior block and
replaces it cleanly. **Always include marker comments** for any
machine-injected content in human-readable files — without them, you
can't tell what's safe to overwrite vs what's hand-edited prose.

Pass 1 is fast: 5,945 cards processed + 5,850 written in ~10 seconds (pure
SQLite + file I/O, no network).

### Pass 2 — LLM fill-gap on misses only

After Pass 1, scan target documents for the "empty" sentinel in the
augmented block (e.g. `_No contacts available in contacts.db..._`). Only
those need LLM work. This is the smoke-before-scale rule applied to
augmentation: don't pay LLM cost for documents where the cheap source
already worked.

For each gap document:

1. Look up the source paper text from any pre-extracted `*_extraction.txt`
   or run pdftotext on the original PDF.
2. Feed the first ~8KB (author block + abstract live in the head — no
   point sending the full 50KB body for a contact extraction).
3. Call cheap-but-capable LLM (llama70 / Haiku 4.5 / Sonnet 4.6 depending
   on judgment depth) with a strict JSON-array schema.
4. Re-render the canonical block with `source: llama70+paper_head`
   instead of `source: contacts.db` so the provenance is visible.

## Coverage projection caveat

**Don't quote coverage from the wrong table.** First pre-flight on the
2026-06-08 run quoted 65-74% DB coverage based on `paper`-table membership;
actual coverage was 42-52% because only `contact_paper` (the join) has
real names+emails. The `paper` table can have an osti_id with zero
`contact_paper` rows — paper metadata extracted, contact extraction
failed. Always count from the join table that has the field you'll
actually display, not from a parent table.

```python
# WRONG — overcounts
ids_with_data = {r[0] for r in con.execute("SELECT DISTINCT osti_id FROM paper")}

# RIGHT — counts only ids with at least one extractable row
ids_with_data = {r[0] for r in con.execute("SELECT DISTINCT osti_id FROM contact_paper")}
```

## Pre-flight gap audit before scaling Pass 2

Before launching the LLM pass, count:

1. How many target documents need Pass 2 (failed Pass 1).
2. How many of those have **available source text** (extraction.txt or PDF).
3. How many have neither.

This produces an honest ETA (`gaps × avg_latency / workers`) and surfaces
the "no source available" bucket early so the user knows the ceiling
isn't 100%. On the 2026-06-08 run: 2,021 gaps, 100% had extraction.txt
available, so no PDF fallback needed.

```python
# Pre-flight pattern
text_index = build_text_index([GOOD/"ALL-PAPERS-*-CARDS", GOOD/"REMAINING-*-CARDS"])
gaps_with_text = sum(1 for oid in gap_ids if oid in text_index)
gaps_with_pdf = sum(1 for oid in gap_ids if oid in pdf_index)
gaps_neither = len(gap_ids) - gaps_with_text - gaps_with_pdf
print(f"gaps={len(gap_ids)}, has_text={gaps_with_text}, has_pdf={gaps_with_pdf}, neither={gaps_neither}")
```

## Filename → ID extraction pitfall

Filename patterns drift across corpus generations. Common shapes for OSTI:

- `1339457_data_card.md` — clean
- `PDF-4_1339457_data_card.md` — older shape, the `PDF-N` prefix has a
  numeric run that's NOT the osti_id
- `arxiv_2207.00611_data_card.md` — arXiv id, no osti_id at all

`re.search(r'(\d{6,8})', name)` matches the **first** numeric run of the
right length, which works because osti_ids are 6-8 digits and `PDF-N`
prefixes have 1-2 digits. But this silently fails on arxiv-style names
(no 6-8 digit run). On the 2026-06-08 run, 35 of 5,945 cards had no
osti_id → bonus pass needed against DOI lookup if you want to cover them.

Pre-flight test:
```python
import re
ID_RE = re.compile(r'(\d{6,8})')
for f in sorted(base.glob("*.md")):
    m = ID_RE.search(f.stem)
    if not m:
        print(f"no_id: {f.name}")
```

## Linked artifact

- `scripts/augment_corpus_with_markers.py` — ready-to-run augmenter that
  encodes both passes (SQLite-driven canonical injection + LLM gap-fill)
  with the marker-comment idempotent shape. Generalized from the
  xCard contacts augmentation. Edit the `fetch_contacts()` /
  `render_block()` / `call_llm()` functions for your specific field.
