# Orphan diagnostic pattern

When a fresh recon set vs an existing-on-disk set produces a surprising
non-empty *orphan* group (have-but-not-in-new-recon), **do not theorize.
Probe the OSTI API for a stratified sample of orphan IDs and let the data
tell you what they are.**

## Why this matters

The default theory ("oh, those must be older papers from before our queried
window") is wrong far more often than it's right. Orphans are more usually:

- **Recon coverage gaps** — one cell of the lab×year grid failed with 429 /
  IncompleteRead / timeout, the script reported success on retry, but a
  page or two of records was dropped.
- **Query-string mismatch** — the historical corpus was fetched with a
  different `research_org` string than what we used in the current recon.
  OSTI matches the exact string in `research_orgs[]`. "Lawrence Berkeley
  National Laboratory (LBNL), Berkeley, CA (United States)" and "Lawrence
  Berkeley National Laboratory" can return overlapping-but-different sets.
- **Sponsor-vs-host distinction** — earlier scripts may have queried
  `sponsor_orgs` instead of `research_orgs`, picking up DOE-SC-sponsored
  papers from non-DOE-SC labs (Harvard, MIT, etc.).
- **Date-window edge cases** — papers with multiple `publication_date`
  candidates (preprint date vs accepted date vs published date) sometimes
  flip between query windows depending on which date field the API uses.

If you assume "they're old" and act on that (e.g. "let's extend the recon
back to 2006"), you'll do the right thing for the wrong reason — or
you'll skip a *real* coverage-gap fix.

## The diagnostic recipe

```python
#!/usr/bin/env python3
"""Probe OSTI API for a stratified sample of orphan IDs."""
import json, urllib.request, ssl, time, random
from pathlib import Path
from collections import Counter

ORPHANS = Path("/tmp/orphan_ids.txt")  # one osti_id per line
N = 80                                  # sample size; 50-100 is the sweet spot

random.seed(42)
ids = ORPHANS.read_text().splitlines()
sample = random.sample(ids, min(N, len(ids)))

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

out = []
for osti in sample:
    url = f"https://www.osti.gov/api/v1/records/{osti}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OSTI-probe/1.0"})
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            data = json.loads(resp.read())
            if isinstance(data, list):
                data = data[0] if data else {}
            pd = data.get("publication_date", "")
            year = (pd.split("/")[-1][:4] if "/" in pd
                    else pd.split("-")[0][:4] if "-" in pd else "")
            orgs = [o.get("name") if isinstance(o, dict) else str(o)
                    for o in data.get("research_orgs", [])]
            out.append({
                "osti_id": osti, "year": year,
                "research_orgs": orgs,
                "sponsor_orgs": [s.get("name") if isinstance(s, dict) else str(s)
                                 for s in data.get("sponsor_orgs", [])][:3],
                "type": data.get("product_type", ""),
            })
    except Exception as e:
        out.append({"osti_id": osti, "error": str(e)[:80]})
    time.sleep(0.1)  # be polite

year_c = Counter(r.get("year", "(?)") for r in out)
org_c = Counter()
type_c = Counter()
for r in out:
    if "error" in r: continue
    for o in r.get("research_orgs", []):
        org_c[o[:60]] += 1
    type_c[r.get("type", "")] += 1

print("=== YEARS ==="); [print(f"  {y:>8}  {n}") for y, n in sorted(year_c.items())]
print("=== ORGS  ==="); [print(f"  {n:>3}  {o}") for o, n in org_c.most_common(20)]
print("=== TYPES ==="); [print(f"  {n:>3}  {t}") for t, n in type_c.most_common()]
```

Runtime: ~15 seconds for 80 IDs. Output tells you immediately whether
orphans are old papers, query-mismatch papers, or coverage-gap papers.

## What to do based on the diagnostic output

| Diagnostic result | Interpretation | Fix |
|---|---|---|
| All orphans cluster in 1-2 (lab, year) cells | Recon coverage gap from a failed cell | Delete the affected `recon/<lab>__<year>.jsonl`, rerun the recon script. |
| Orphans span many labs/years but share an org string variant | Query-string mismatch | Add the variant to the recon `LABS` list and rerun. Dedupe via osti_id. |
| Orphans have `sponsor_orgs` containing DOE-SC but `research_orgs` from non-SC labs | Historical script queried sponsor not host | These belong in a separate "SC-funded" corpus, not the SC-lab corpus. Tag and stash, don't try to "fix" the recon. |
| Orphans are genuinely pre-window (pre-2016) | Original corpus was broader than current recon | Decision is product: extend window or accept orphans as-is. |
| Mixed | Multiple causes | Run the diagnostic on a fresh stratified sample within each bucket. |

## Worked example (2026-06-07)

- Existing corpus 67,119 PDFs ∩ new candidates 176,159 = 65,553 overlap.
- Existing - new = **1,566 orphans**.
- Sampled 80; 70 successful API responses, 10 404s (orphan PDF still
  on disk but record deleted from OSTI — happens for retracted papers).
- **All 70 = year 2020, all = LBNL, all = Journal Article.**
- Earlier recon log: `ERR  Lawrence Berkeley National Laborato 2020
  page412: IncompleteRead(139264 bytes read)`.
- Conclusion: orphans are the dropped tail of the LBNL 2020 cell.
- Fix: rerun the cell with `time.sleep(2)` between pages.
- The wrong-theory I almost committed to: "they're pre-2016 papers, let's
  extend the window back to 2006." Extending the window is still a good
  separate decision — but it would NOT have recovered the orphans.

## Generalization

This pattern applies to any "set A vs set B, why are some in A not in B?"
debugging where each member of the set has a free per-ID metadata API:

- arXiv: `https://export.arxiv.org/api/query?id_list={id}`
- CrossRef: `https://api.crossref.org/works/{doi}`
- HuggingFace Hub: `https://huggingface.co/api/models/{id}`
- OpenAlex: `https://api.openalex.org/works/{id}`

The recipe is always: stratified random sample → per-ID API call → cluster
the result fields → read the bucket distribution → form theory grounded
in data. **Time-bound it to ~5 minutes**; if it takes longer, you're
probably trying to do the *fix* in the diagnostic, not just the *diagnosis*.
