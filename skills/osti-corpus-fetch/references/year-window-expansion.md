# Year-window expansion — rebuild candidates and diff against prior set

Use when Rick asks to extend the recon window backwards (or forwards), or when you realize the existing pool was queried with a too-narrow `YEARS` range. The recon is idempotent so re-running with a wider range is safe; the work is in rebuilding the candidate pool and figuring out **what's actually new** vs the prior pool so you can stage the delta without re-doing fetched work.

## The pattern

Recon script (`scripts/recon_sc_labs.py`) has a top-of-file constant:

```python
YEARS = list(range(2016, 2027))   # original window
```

Editing this in-place on the remote host and re-launching is the canonical expansion move. `sed` it via ssh, kick off with `nohup`, walk away. The script will *cache-hit* every existing `recon/<lab>__<year>.jsonl` and only fetch the new year cells. A 10-year backfill on `cels-oss120` runs in ~30-60 min depending on lab pagination depth.

Verify the edit took before the long run starts:

```bash
ssh cels-oss120 'cd ~/code/osti-replication-candidates && \
  sed -i "s|YEARS = list(range(2016, 2027))|YEARS = list(range(2006, 2027))|" recon_sc_labs.py && \
  grep YEARS recon_sc_labs.py | head -2'
```

## Rebuild candidates from the expanded recon

Once the recon finishes (final log line is `GRAND TOTAL records: N`), rsync the cells home and rebuild the deduped candidate pool. The fragile part is the rsync invocation — `~/code/...` quoting via ssh has bitten me; bare `code/...` resolves relative to remote `$HOME` and Just Works:

```bash
cd ~/code/osti-replication-candidates && \
  rsync -az cels-oss120:code/osti-replication-candidates/recon/ recon_v2/
```

(Avoid `--info=stats1` here — older rsyncs on macOS reject the long flag and dump the full help text instead of failing fast. Plain `-az` is enough.)

Then dedupe + filter + DOI-count + diff. The template script is short enough to inline — copy-modify as needed:

```python
import json, glob, os

recon_dir = os.path.expanduser("~/code/osti-replication-candidates/recon_v2")
files = sorted(glob.glob(os.path.join(recon_dir, "*.jsonl")))

seen, papers, code_skip = set(), [], 0
for fp in files:
    with open(fp) as f:
        for line in f:
            try: r = json.loads(line)
            except: continue
            oid = str(r.get("osti_id", ""))
            if not oid or oid in seen: continue
            if oid.startswith("code-") or oid.startswith("dataset-") or oid.startswith("biblio-"):
                code_skip += 1; continue
            seen.add(oid); papers.append(r)

print(f"Unique papers: {len(papers):,}")
print(f"Non-paper IDs filtered: {code_skip:,}")
print(f"With DOI: {sum(1 for p in papers if p.get('doi')):,} ({100*sum(1 for p in papers if p.get('doi'))/len(papers):.1f}%)")

with open("/tmp/candidates_papers_v2.jsonl", "w") as f:
    for p in papers: f.write(json.dumps(p) + "\n")

# Diff against prior pool
prev = "/tmp/candidates_papers_only.jsonl"  # or whatever the prior version was named
if os.path.exists(prev):
    prev_ids = {str(json.loads(l).get("osti_id","")) for l in open(prev)}
    new = seen - prev_ids
    dropped = prev_ids - seen
    print(f"\nPrior pool: {len(prev_ids):,}")
    print(f"NEW (v2-v1): {len(new):,}")
    print(f"DROPPED (v1-v2): {len(dropped):,}")
```

The non-paper filter (`code-` / `dataset-` / `biblio-`) is the same filter from the SKILL.md `Pitfalls` section — apply it here at rebuild time so every downstream script gets a clean pool.

## Empirical scale (2026-06-07 expansion)

For calibration when sizing future expansions:

| Recon scope | Total records | Unique papers (post-filter) | Notes |
|---|---|---|---|
| 10 labs × 2016-2026 | ~186K | 174,329 | Initial pool, code-* filter dropped 1,830 |
| 10 labs × 2006-2026 | 422,426 | 403,828 | After +10 years backfill; **+229,499 new IDs**, 0 dropped, DOI 87.1% |

The shape worth noting: doubling the year window **more than doubled** the paper count because pre-2016 was unindexed in the prior pool. Expect similar non-linearity if future expansions include other DOE labs (NNSA tier, EERE tier) or pre-2006 archival material. Recon time scales roughly linearly with cells (lab × year) and not with paper count, because OSTI API pagination is fast per-page.

## In-flight run discipline — DO NOT interrupt

When a recon expansion produces a fresh, larger candidate pool while existing bulk_fetch / classifier / extraction runs are mid-stream against the old pool, **the default is to let in-flight runs finish on v1**. Reasons:

1. Bulk_fetch progress is non-trivial to checkpoint cleanly across an input-list swap. The log files (`bulk_fetch.log.jsonl`) reference IDs that are valid in both pools, but the percent-done semantics break and ETAs become meaningless.
2. The classifier and code extractor write per-record JSONL — re-running them on v2 will idempotently skip records already in their output, so launching the v2 run is just an append; no reason to kill v1.
3. The new IDs aren't going anywhere. Stage the v2 delta as queued follow-up work.

Concrete staging (do this in parallel with the in-flight runs, don't wait for them):

```bash
# 1. Compute the v2-only delta against v1 + existing fetched PDFs
python3 -c "
import json
v2 = {str(json.loads(l)['osti_id']) for l in open('/tmp/candidates_papers_v2.jsonl')}
v1 = {str(json.loads(l)['osti_id']) for l in open('/tmp/candidates_papers_only.jsonl')}
new_ids = v2 - v1
open('/tmp/v2_new_ids.txt','w').write('\n'.join(sorted(new_ids)))
print(f'v2-only delta: {len(new_ids):,}')
"

# 2. Push to fetch host as a separate file; the v1 bulk_fetch keeps running unaffected
scp /tmp/v2_new_ids.txt cels-rbdgx2:~/v2_new_ids.txt
```

When v1 bulk_fetch wraps, launch the v2 fetch with the same script pointed at `v2_new_ids.txt`. Don't merge the two input lists into one mega-list mid-run.

The only time to interrupt is if v1 is *broken* (e.g. the first-200-records ok-count is 0 from the `code-*` pollution pitfall in SKILL.md). Healthy progress = don't touch it.

## Source for further detail

This session's transcript when the 2006-2015 backfill landed is in the session DB; search for the 422,426-record GRAND TOTAL or the 229,499-new-IDs diff if you need to reconstruct the full state. The `recon_v2/` directory on m1 at `~/code/osti-replication-candidates/recon_v2/` was the post-expansion staging area used for the rebuild.
