# Recovering missing fulltext from open-access mirrors — measure the ceiling first

When a local PDF corpus has gaps (extractor errors, missing files, image-only
scans), the obvious next move is "go fetch the OA copy from somewhere else."
This note is about how to do that **without wasting an afternoon on a tool
choice** when the actual bottleneck is upstream.

## TL;DR

**Before you adopt or wrap any OA-recovery tool (PullR, scihub-py, paperscraper,
unpaywall-cli, etc.), spend 30 minutes on a 50-paper measurement to answer:**

1. What fraction of the gap papers have a DOI?
2. What fraction of those DOIs resolve to an OA PDF anywhere (Unpaywall / S2 /
   CrossRef)?
3. What fraction of those OA URLs are on hosts that **actually serve** the PDF
   to a scripted GET (not a 403 / Cloudflare challenge / "content unavailable"
   landing page)?

The product of those three numbers is your real recovery ceiling. If it's
under ~20%, no tool choice changes that — the OA landscape is the limit, not
the tool. Pick the cheapest path that hits the ceiling and move on; don't
spend a day comparing tools that all share the same upstream.

## Worked example (OSTI 8K failed-recovery set, 2026-06-06)

Sample = 50 random papers from `recovery_log.jsonl` where the local PDF was
missing/empty and our 4-tier recovery (PURL + Crossref + arXiv-DOI + arXiv-
title) returned FAILED. Direct S2 lookup by DOI, no API key, 1.1s between
requests:

| Outcome                              | Count | %   |
|--------------------------------------|------:|----:|
| No DOI in metadata                   |     2 | 4%  |
| S2 lookup failed (4xx / rate-limit)  |    35 | 70% |
| S2 hit, no OA PDF advertised         |     0 | 0%  |
| S2 hit, OA PDF found                 |    13 | 26% |

Of the 13 OA hits, the host breakdown:

| Host                       | Count | Downloadable? |
|----------------------------|------:|---------------|
| escholarship.org           |     5 | **No** (HTTP 403, 919-byte HTML reject page, Cloudflare) |
| arxiv.org                  |     1 | Yes |
| ncbi.nlm.nih.gov (PMC)     |     1 | Yes |
| europepmc.org              |     1 | Yes |
| iopscience.iop.org         |     1 | Maybe (publisher) |
| academic.oup.com (Oxford)  |     1 | Maybe (publisher) |
| osti.gov                   |     1 | Already tried (this is the source) |
| pureadmin.qub.ac.uk        |     1 | Probably (institutional) |
| eprints.whiterose.ac.uk    |     1 | Probably (institutional) |

So the **real S2-tier ceiling is ~8/50 = 16%**, not 26%. The escholarship
papers will appear in any OA index that uses S2/CrossRef/Unpaywall data
(they all source from the same place), and the 403 will hit every tool the
same way. The escholarship.org Cloudflare rule does not care which Python
library is sending the request.

The 70% "S2 lookup failed" is mostly anonymous-tier rate-limiting; with an
S2 API key it drops to ~10%. Worth requesting one before the production run
if you're going past ~500 lookups.

## What this means for tool selection

In the OSTI case I had three options on the table:

- **(A) Wrap PullR** — has S2 + scraping + LLM-citation-parsing built in.
  But the LLM-parsing is for *unstructured citations* — we already have
  clean DOIs. The actual S2-search + openAccessPdf-resolve is ~40 lines.
- **(B) Direct S2 driver** — write the 40 lines, hit the same ~16% ceiling
  as PullR would.
- **(C) Unpaywall + arxiv-title fallback** — Unpaywall is a different OA
  resolver with stronger green-OA coverage (it specifically looks at
  institutional/preprint mirrors instead of just publisher-declared OA).
  Probably 25-35% ceiling because some escholarship-only papers have a
  preprint mirror Unpaywall knows about that S2 doesn't.
- **(D) Skip recovery, fall back to abstract-only judgment** — keep the
  Stage 2 LLM pass, accept that ~10-15% of papers go through judgment with
  abstract only.

The right call **is the one that maximizes ceiling per hour invested**, not
the one that's "the most thorough." For an 8K paper backlog where Stage 1
triage is already running and abstracts ARE going to be sufficient for
~95% of triage decisions:

- (A) PullR-wrap: 2-4h to integrate, ~16% ceiling. **Wrong call.**
- (B) Direct S2: 1h to write, ~16% ceiling. Marginal.
- (C) Unpaywall: 1h to prototype, 25-35% ceiling. Best ROI.
- (D) Skip: 0 effort, 0% recovery but 95%+ judgment unaffected. Default.

The thing to NOT do is pick (A) because the tool exists and looks shaped
right. PullR's main feature (LLM-citation-parsing) is irrelevant when the
input is already structured metadata, and the underlying S2+scraping layer
is short enough to inline.

## The 30-minute pre-flight recipe

Before adopting any OA-recovery tool, run this:

```python
# Pick 50 random gap papers with a DOI, hit S2 directly, tally outcomes.
import json, urllib.parse, urllib.request, time, random, collections, ssl

random.seed(42)
# Load gap list (e.g. failed recoveries, missing fulltext, etc.)
gap_papers = [...]  # each has at minimum {osti_id/arxiv_id/etc, doi, title}
sample = random.sample(gap_papers, 50)

hosts = collections.Counter()
no_doi = s2_fail = no_oa = 0
ctx = ssl.create_default_context()

for r in sample:
    doi = (r.get('doi') or '').strip()
    if not doi:
        no_doi += 1
        continue
    enc = urllib.parse.quote(doi, safe='')
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{enc}?fields=openAccessPdf"
    req = urllib.request.Request(url, headers={'User-Agent':'corpus-recovery/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            data = json.loads(resp.read())
        pdf = (data.get('openAccessPdf') or {}).get('url')
        if not pdf:
            no_oa += 1
        else:
            hosts[urllib.parse.urlparse(pdf).netloc] += 1
    except Exception:
        s2_fail += 1
    time.sleep(1.1)  # anonymous tier; ~1 req/s safe

print(f'no DOI:      {no_doi}')
print(f'S2 failed:   {s2_fail}')  # ~70% anonymous, ~10% w/ key
print(f'S2 no OA:    {no_oa}')
print(f'S2 OA hit:   {sum(hosts.values())}')
print('--- hosts ---')
for h, n in hosts.most_common(15):
    print(f'  {n:3d}  {h}')
```

Then **manually test a curl GET on 3-5 of the OA URLs** to spot the
Cloudflare/403 hosts. The 919-byte HTML rejection page is the escholarship
signature on this corpus; other publishers have their own.

```bash
for URL in $oa_urls; do
  curl -sL -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15' \
       --max-time 20 -o /tmp/x.pdf \
       -w "HTTP %{http_code}  type=%{content_type}  size=%{size_download}\n" "$URL"
done
```

If `content_type=text/html` and `size < 5000`, it's a reject page — count
that host as 0% downloadable for your real-ceiling math.

## When PullR (or similar tool-wrapping) IS the right call

This note isn't anti-tool. PullR is well-shaped for its actual job:
**you have a directory of PDFs whose references you want to expand into a
research collection**, or **you have a raw citation list (BibTeX, scraped
references section) that needs LLM-parsing into structured queries**.

That's not the OSTI-gap-recovery shape. Our gap papers have clean
structured metadata; we don't need the LLM-parsing step. We just need the
S2 / Unpaywall / arxiv lookup, and that's short enough to inline.

The general rule: **if a tool's main feature does work you don't need,
its remaining features probably aren't worth the wrapping overhead.**
Inline the 30 lines.

## Pitfall pattern (added to SKILL.md)

**"This tool is shaped right for the problem" is not the same as "this tool
is the cheapest path to the ceiling."** Always measure the ceiling first
(30 minutes), then pick the cheapest implementation that hits it. Tools
look more appealing than they are because you only see their feature list,
not their integration cost vs the inline alternative.
