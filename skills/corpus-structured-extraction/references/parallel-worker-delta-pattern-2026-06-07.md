# Parallel-worker delta pattern (mid-run input expansion)

When recon or upstream metadata work discovers that the candidate set is much larger than originally thought — and a long-running fetch/classify/extract job is already in flight on the original set — the right move is to leave the running worker alone and launch a **second** worker on the set-difference.

This was discovered 2026-06-07 on the OSTI corpus expansion: the original 174,329-candidate set ballooned to 403,828 when recon extended from 2016-2026 back through 2006-2015. A bulk fetcher was already 17K records into the 108K v1-missing list on cels-rbdgx2, and a Genesis 22-topic classifier was running on the same v1 set on m1.

## Why not restart with the v2 list

- The running fetcher has an in-memory ID list fixed at process start. Restarting means starting from scratch unless `--resume` reads the log perfectly, which is fragile.
- The running classifier likewise. Even with skip-already-done logic, the process has to re-parse the full new candidate file and re-build the skip set.
- The fetcher's `year_map` is loaded once at start; new IDs would have no year entry without a meta-file swap.
- Stopping a healthy job that's producing useful output to "upgrade" it usually trades a known-good 2-hour run for an unknown 2.5-hour run with a higher restart-risk.

## The pattern

1. **Compute set-difference once, locally.**

   ```python
   v1_ids = {json.loads(l)["osti_id"] for l in open("/tmp/candidates_v1.jsonl")}
   v2_ids = {json.loads(l)["osti_id"] for l in open("/tmp/candidates_v2.jsonl")}
   new_ids = v2_ids - v1_ids
   open("/tmp/missing_v2_additions.txt","w").write("\n".join(sorted(new_ids)))
   ```

2. **Build the v2 metadata file** so the new worker's `year_map` covers the new IDs (and only the new IDs, to keep memory small):

   ```python
   year_map = {}
   for line in open("/tmp/candidates_v2.jsonl"):
       r = json.loads(line); year_map[str(r["osti_id"])] = r.get("year","unknown")
   with open("/tmp/candidate_meta_v2.jsonl","w") as f:
       for oid, yr in year_map.items():
           f.write(json.dumps({"osti_id": oid, "year": yr}) + "\n")
   ```

3. **scp delta + meta to the worker host.**

4. **Clone the worker script with patched LOG/META/IDS paths.** Single Python one-liner does the substitution:

   ```bash
   cp bulk_fetch.py bulk_fetch_v2.py
   python3 -c "
   p = open('bulk_fetch_v2.py').read()
   p = p.replace('missing_papers_only.txt', 'missing_papers_v2_additions.txt')
   p = p.replace('bulk_fetch.log.jsonl',    'bulk_fetch_v2.log.jsonl')
   p = p.replace('candidate_metadata.jsonl','candidate_metadata_v2.jsonl')
   open('bulk_fetch_v2.py','w').write(p)
   "
   diff <(grep -E '^(IDS|LOG|META)' bulk_fetch.py) <(grep -E '^(IDS|LOG|META)' bulk_fetch_v2.py)
   ```

   The diff confirms the three paths swapped and nothing else.

5. **Launch the v2 worker as a fresh detached process** (with `< /dev/null` per the standard pitfall in SKILL.md):

   ```bash
   nohup python3 -u bulk_fetch_v2.py > bulk_fetch_v2.stdout.log 2>&1 < /dev/null & disown
   ```

6. **Apply the same pattern to the classifier** (or any other long-running consumer of the candidate set). Patch a `<x>_v2add.py` clone with:

   ```python
   CANDIDATES = Path("/tmp/candidates_v2_additions.jsonl")
   OUT        = ROOT / "classifications_v2add.jsonl"
   ```

   Run it in parallel to the v1 classifier. Both write to disjoint output files; merge at the end with `cat classifications.jsonl classifications_v2add.jsonl > classifications_merged.jsonl`.

7. **At end-of-run, merge log JSONLs and PDF/output dirs.** Simple cat for jsonls; rsync (`--ignore-existing`) for PDF dirs. No deduplication needed because the two workers' input lists are by construction disjoint.

## When this pattern does NOT apply

- If the running worker's output is consumed in real-time by a downstream stage that assumes a single source-of-truth file, you have one input stream — fix the upstream by relaunching, don't fork.
- If the v1 and v2 sets overlap substantially (>20%), the duplicate work isn't worth the orchestration overhead — restart with v2 and accept the loss.
- If the worker is doing irreversible state mutation (e.g. writing to a database with unique-key constraints), two workers will race. File-append + post-hoc merge is fine; concurrent DB writes need coordination.

## Concrete numbers from the worked example (2026-06-07)

- v1 candidates: 174,329 (recon: 2016-2026, 10 DOE-SC labs)
- v2 candidates: 403,828 (recon: 2006-2026, same 10 labs)
- v2 - v1 delta: 229,499 NEW IDs
- v1 fetcher state at fork: 18,800 / 108,776 done, ~12 req/s, ETA ~2hr
- v2 fetcher launched: 229,499 todo at ~12 req/s, ETA ~5hr
- v1 classifier state: ~5K of 174K done, ~5 req/s, ETA ~8hr
- v2 classifier launched: 229,499 todo at ~5 req/s, ETA ~12hr
- Total elapsed extra wall-clock: ~3 min (script clone, scp, launch)
- Wasted work on v1 jobs: zero
