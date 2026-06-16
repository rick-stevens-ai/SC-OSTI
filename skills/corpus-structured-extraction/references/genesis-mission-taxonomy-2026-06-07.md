# Genesis Mission classification axis (22 topics)

**Locked 2026-06-07 by Rick.** Use this taxonomy for any IMPLICIT-MODELS
work or cross-corpus classification of DOE-SC papers/reports/cards against
Genesis Mission challenge areas.

## The axis

**22 topics = 21 Genesis Mission NOFO Challenge Areas + 1 US-Japan addition.**

Source for the 21:
- Authoritative PDF: `~/Dropbox/GENESIS-RFA/NOFO-LHP-DE-FOA-0003612.pdf`
- Structured: `~/Dropbox/GENESIS-RFA/GM-NOFO-RFA-AREAS.xlsx`
  (columns: Topic | Challenge Area | Subtopic | Office | Focus Area)
- AI-extracted summary: `~/Dropbox/GENESIS-RFA/NOFO-AREAS-AI-CLAUDE.pdf`
- Cached parsed JSON: `/tmp/genesis_areas.json` (21 areas with 5-8 focus
  areas each, tagged by sponsoring DOE office)

Source for topic 22: **US-Japan collaboration** (specific source document
not yet located in `~/Dropbox/GENESIS-RFA/`, `~/Dropbox/RIKEN-Tokyo-Friday/`,
or `~/code/lighthouse-archive/`). Rick added it from his own knowledge of
the bilateral effort. Ask him for the source doc if a citation is needed.

## What is NOT in the axis

- **DOE-IN (Intelligence) list — dropped.** Was one of the 4 Lighthouse
  tracks (7 submissions) but is NOT used for IMPLICIT-MODELS classification.
- **NNSA Lighthouse Security track — dropped.** Was another Lighthouse track
  (9 submissions) but also not part of the public NOFO + not on the
  classification axis.
- The Lighthouse "40 raw / 26 deduped / 21 final" lineage is **historical
  crosswalk only** — useful for memorability and for tracing how a topic
  evolved, but NOT the classification axis. Don't add a "Lighthouse track"
  column to the classifier output.

## The 21 NOFO topics (canonical names)

Topics 1-17 = National Science & Technology Challenges (substantive science).
Topics 18-21 = cross-cutting platform topics.

1. Reenvisioning Advanced Manufacturing
2. Scaling Biotechnology Revolution
3. Critical Minerals Supply
4. Nuclear Energy Faster/Safer/Cheaper
5. Accelerating Fusion
6. Nuclear Restoration
7. Quantum Algorithms with AI
8. Quantum Systems for Discovery
9. Microelectronics
10. Data Centers
11. AI-Driven Autonomous Labs
12. Materials with Predictable Functionality
13. Particle Accelerators
14. Unifying Physics Quarks→Cosmos
15. US Water for Energy
16. Grid for American Economy
17. Subsurface Strategic Energy
18. HPC Code Curation/Translation
19. AI for Scientific Reasoning
20. Cybersecurity for AI-driven Workflows
21. AI in Fluid Flow for Energy Components

## Topic 22 (US-Japan)

22. **AI for Math and Computer Science**

No focus-topic subtree available yet (source doc TBD). For the first-pass
classifier, treat topic 22 as a single bucket; refine if/when the source
surfaces.

## How to use this for a classifier

1. Load `/tmp/genesis_areas.json` for the 21-topic + 99-focus structure.
2. Append the synthetic row for topic 22:
   ```json
   {"topic_id": 22, "challenge_area": "AI for Math and Computer Science",
    "office": "US-Japan", "focus_areas": []}
   ```
3. Prompt the LLM with the full topic list (title + 1-line summary) and ask
   for top-3 best matches per paper with confidence + brief rationale.
4. Recommended judge: CELS llama70 (free, 4 req/s, ties Sonnet 4.6 on
   short-answer classification per the bake-off pattern in this skill).
5. Optionally include "DOE-SC Office" (BES/BER/FES/NE/ASCR/HEP/NP) as a
   secondary axis since each topic has a sponsoring office in the NOFO —
   this helps when papers span multiple topics.

## Pitfall: don't propagate stale topic counts

I have parroted "26 Challenge Areas / 100 Focus Topics" in past sessions —
that figure is **NOT current**. It corresponds to the deduped SC+Energy
Lighthouse subset from late 2024, not the final NOFO. Use 21 (or 22 with
the US-Japan addition) going forward; correct any stale "26" mention
when you see it.

## Pitfall: never hand-paraphrase the topic list — always rebuild from the xlsx

When prepping a classifier or any taxonomy artifact, **load the topics from
`~/Dropbox/GENESIS-RFA/GM-NOFO-RFA-AREAS.xlsx` programmatically** (via
openpyxl) rather than hand-typing them from memory. The names look benign
but they are NOT what you remember:

- Topic 6 is **"Transforming Nuclear Restoration and Revitalization"**
  (waste cleanup, decommissioning, EM mission) — NOT "Nuclear Stockpile" or
  any weapons topic. There are ZERO weapons topics in the 21-area NOFO.
  Hand-paraphrasing as "Nuclear Stockpile / Weapons" and then "dropping" it
  per the no-weapons rule will silently shrink the axis to 21 instead of 22.
- Topic 18 is "HPC Code **Curation, Translation, and Development**" — the
  ", and Development" tail matters because it's the only NOFO topic that
  explicitly funds code creation rather than reuse.
- Topic 20 is "Cybersecurity for AI-driven **Science** Workflows" — the
  "Science" word distinguishes it from generic AI-security work.
- Topic 3 is "Securing **America's** Critical Minerals **Supply**" (not
  "Supply Chain") — the noun matters for keyword classifier prompts.

Failure case 2026-06-07: I built a 22-element list from memory, dropped
topic 6 as "Nuclear Stockpile (weapons, RESERVED)" per Rick's no-weapons
rule, and ended up with 21 active topics instead of 22 — the off-by-one was
only caught because Rick had said "22 topics" two messages earlier. Fix
recipe is one openpyxl block:

```python
import openpyxl, json
wb = openpyxl.load_workbook("/Users/stevens/Dropbox/GENESIS-RFA/GM-NOFO-RFA-AREAS.xlsx", data_only=True)
ws = wb.active
topics = {}
for row in ws.iter_rows(values_only=True):
    if not row or not row[0] or str(row[0]).strip() == "Topic": continue
    try: tid = int(str(row[0]).strip())
    except ValueError: continue
    name   = (row[1] or "").strip() if len(row) > 1 else ""
    office = (row[3] or "").strip() if len(row) > 3 else ""
    focus  = (row[4] or "").strip() if len(row) > 4 else ""
    if tid not in topics:
        topics[tid] = {"id": tid, "name": name, "office": office, "focus_areas": []}
    if focus: topics[tid]["focus_areas"].append(focus)
# add topic 22 as a synthetic entry
topics[22] = {"id": 22, "name": "AI for Math and Computer Science",
              "office": "ASCR/US-Japan", "focus_areas": []}
active = [topics[i] for i in sorted(topics)]
assert len(active) == 22, f"expected 22 topics, got {len(active)}"
json.dump(active, open("topics_22.json", "w"), indent=2)
```

Run this and `assert len(active) == 22` before passing the list to a
classifier prompt. Anything that fails that assertion has either a parser
bug or a topic-list misread.

**Python interpreter note:** openpyxl is installed under `/opt/anaconda3/bin/python3`
(3.8) on this M1 — `/usr/bin/python3` and `/opt/homebrew/bin/python3` will
both fail with `ModuleNotFoundError`. Use the anaconda one for any xlsx
work on this host.

## Pitfall: Dropbox metadata walks time out

`find ~/Dropbox -iname "*japan*"` and similar recursive walks routinely
hit the 60s tool timeout on this M1 mini. Use `-maxdepth 3` and one
subtree at a time (`~/Dropbox/GENESIS-RFA/`, `~/Dropbox/RIKEN-Tokyo-Friday/`)
rather than a full-tree walk. Also see the "Dropbox 0-byte sync
corruption" pitfall in the parent SKILL.md — recovery via rsync from
m3acbook-pro (Tailscale <tailnet-m3acbook>) is the escape hatch.

## Related artifacts

- `~/code/implicit-models/PROJECT_BRIEF.md` — IMPLICIT-MODELS project brief
  that depends on this axis for its "code/data/people × challenge-area"
  coverage matrix.
- `~/code/lighthouse-archive/` — recovered Lighthouse master docs for
  historical lineage if anyone asks "where did topic N come from."
