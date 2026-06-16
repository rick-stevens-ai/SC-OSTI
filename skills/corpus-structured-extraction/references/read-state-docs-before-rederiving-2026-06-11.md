# Read state docs before re-deriving from raw data

**Date:** 2026-06-11
**Project:** OSTI corpus recovery — multi-week, multi-host, multi-session
**Failure shape:** Fresh session computed wrong headline numbers by re-deriving
from raw artifacts instead of reading the project's canonical state documents.

## The setup

Rick: "please give me a situation report" → "for the revised update state"

My context had a compaction summary from prior sessions claiming:
- Recon: 407,704 unique osti_ids
- Have fulltext: 58,445 .txt files
- Gap: 349,411 papers (85.7%)
- cels recovery ok count: 17,978 (from a "Morning hand-off" line)

These numbers were treated as authoritative state.

## What I did wrong

Built a 10-item full-audit todo and started executing it from raw artifacts:
1. `tar tzf` per-year Cherry6TB tarball listings (slow, backgrounded)
2. `find fulltext/ -size N` for quality buckets
3. `wc -l` on `fulltext_meta.jsonl` for status distribution
4. ssh probes to uicgpu + rbdgx2 for OCR state
5. About to compute LBNL-49%-of-gap headline from recon vs fulltext set math

Computed several numbers, then noticed `OSTI_PAPERS_COVERAGE_2026-06-10.md`
and `MORNING_PICKUP.md` in the project root via `ls -lat | head`.

## What the canonical docs said

Both documents (5-12 KB each, dated yesterday + this morning) had complete
ground truth that overrode my session state:

| My session state | Canonical state | Why mine was wrong |
|---|---|---|
| 407K papers | 238K papers | I was counting 169K Materials Project dataset entries (DOI prefix `10.17188`) and other non-paper product types as papers |
| 349K gap (85.7%) | 117K true gap (49.2%) | Derived from wrong universe count |
| 17,978 cels ok | 23,613 cels ok (live DB query) | My state was 12+ hours stale; overnight delivered +5,635 |
| Sweep coordinator unknown | DONE last night | Hand-off doc explicitly said "DONE: fired round-2 cleanly, exited" |
| 4,207 OCR'd | 4,207 OCR'd | Match — coincidence not validation |

The canonical docs ALSO had:
- Per-bucket breakdown (8,119 zero-byte stubs, 1,837 image-only needing OCR)
- Realistic ceiling math (47% → ~65% with EZproxy + TDM key)
- Pending-action list (EZproxy login on prokko's physical screen)
- Worker-by-worker overnight result table (7 distinct workers, 5 winners and 2 dead-ends)
- TDM key request draft ready to send

None of which my from-scratch audit would have rediscovered in less than an hour.

## The cost of re-deriving

Wasted tool calls before the catch: ~10 (one tarball-listing background job
still running, four `find` size-buckets, three jsonl-parse passes, two ssh probes).

Worse than the wasted calls: the headline I was building (`349K gap`,
`LBNL = 49% of gap`) would have been off by 169K papers because the recon
list includes non-paper product types. Had I posted that as the situation
report, Rick would have had to correct the universe count before any plan
discussion. Same trap as past `407K` headlines from earlier sessions —
**there's a recurring pull to re-derive from `recon_v2/` jsonl files
because they're easy to grep, even when a `papers_manifest.tsv` exists
that has already done the product-type filter.**

## The recovery

Once I read the docs (~30 seconds of read_file calls), I:
1. Updated the report shape to use the canonical 238K / 47.3% numbers
2. Used my live `sqlite3` query against cels DB to refresh ok-count to
   23,613 (the only number worth computing live — DB state advances every
   minute as workers commit)
3. Dropped the 10-item full audit entirely — most of it had been done
   yesterday and the answers are in the docs
4. Wrote the situation report in ~30 lines synthesizing canonical state
   + live ok-count + the 1-2 actually-unknown things (EZproxy not yet
   logged in, marker idle 0 procs)

Total recovery: 3 read_file calls + 1 live ssh sqlite3.

## Generalization — the rule

For any project that has been running across multiple days/sessions with
artifacts evolving, the canonical state documents are the FIRST thing to
load, not the LAST.

### Phase 0 of any "status of project X" session

Before any audit, computation, or recount:

```bash
ls -lat <project_dir>/*.md 2>/dev/null | head -10
ls -lat <project_dir>/ | head -20          # catches non-.md handoffs too
ls <project_dir>/daily_deltas/ 2>/dev/null # date-stamped per-day records
```

Look for filenames matching any of:
- `MORNING_PICKUP*`, `HANDOFF*`, `STATUS*`, `COVERAGE_*`, `*_REPORT*`
- `*_PLAN.md`, `*_DESIGN.md`
- Date-stamped `<TOPIC>_YYYY-MM-DD.md`
- `daily_deltas/YYYY-MM-DD.jsonl`

Read every match from the last 7 days. They WILL have ground truth that
your raw-data audit will get wrong or take an hour to recompute.

### When to bypass and re-derive anyway

Only when the canonical doc is older than the data you're about to report
on AND the data changes fast enough to matter. Example: if `COVERAGE_2026-06-10.md`
says "ok count: 17,978" and you're reporting Thursday afternoon on a recovery
pipeline that's been running overnight, **the only number worth recomputing
is the rapidly-changing one** (live DB query for ok-count). Everything else
in the canonical doc is still authoritative.

### When NO canonical doc exists

This is itself a signal — either the project is genuinely new, or it's
been worked on without proper hand-off discipline. Offer to create one
at the end of the session so the next agent starts from ground truth
instead of raw-data archeology.

## Cross-check: when raw-data audit IS the right move

This rule does NOT say "never compute from raw data." Genuine triggers:
- The canonical doc is >7 days old AND the project has been actively worked
- The canonical doc and the data show CONTRADICTION (canonical says X
  recovered, raw `ls | wc -l` shows Y ≠ X — investigate)
- The user explicitly asks "audit / verify / recount / sanity-check the
  state" rather than "what's the status"
- Building a NEW summary that wasn't in scope of the prior canonical doc

For "what's the status?" / "situation report" / "where are we" — read the
docs first, recompute only the fast-changing rapidly-stale numbers.

## Cross-reference

Same family as the "directory file count doesn't tell you what kind of
artifact" pitfall already in this SKILL.md (the `cards/` subdirectory was
contact-cards not paper-cards, also caught only after sampling). Both
share root cause: **propagating session-state claims about a directory
or count without re-grounding against the actual filesystem / canonical
documentation.** The fix in both cases is one cheap pre-flight step
(`ls` + `head -3` for one, `ls -lat *.md` + `read_file` for the other)
that takes seconds and prevents wrong-number reports.
