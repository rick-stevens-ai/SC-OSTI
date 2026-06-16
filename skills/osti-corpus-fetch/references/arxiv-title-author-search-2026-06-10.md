# arXiv title+author search vs DOI search — the 50x recovery-rate breakthrough

## Context

2026-06-10, Phase C of OSTI corpus refresh. After Phase C Unpaywall pass hit ~11%
recovery, fan-out workers were built to attack residual failed buckets. One worker
(`arxiv_fetcher.py`) targeted ~6,600 papers with arXiv-friendly DOI prefixes (10.1103
APS, 10.1140 EPJ, 10.1088 IOP, 10.3847 AAS, 10.1093/mnras, 10.1051 EDP, 10.1063 AIP,
10.1126 Science, 10.1073 PNAS, 10.1146 Annual Reviews) by querying arXiv API with
`search_query=doi:"<DOI>"` and downloading the matching preprint.

After 425 attempts: **4 successes, 421 misses (~0.9% hit rate).**

Rick's correction: "arxiv should be searched by title and authors not DOI."

Rebuilt as `arxiv_title.py` using `ti:"<title>"+AND+au:<lastname>` against the same
target pool. Smoke test of 20 papers: **10 hits, 10 misses (50% hit rate).** All 10
hits were real, on-topic preprints matching the OSTI paper (manually validated).

50x recovery-rate improvement from changing the query shape.

## Why DOI search fails on arXiv

The publisher mints the DOI **after** the preprint is posted to arXiv. The author
sometimes goes back and adds the DOI to the arXiv metadata, but most don't. arXiv's
metadata index treats DOI as an optional foreign-key field that's empty for the
majority of preprints. A DOI search hits only those preprints whose authors did the
manual back-fill.

For Argonne/LBNL/SLAC/Fermilab physics papers — the bulk of OSTI's APS/HEP/MNRAS
corpus — DOI back-fill rate is well under 5%. The 0.9% headline hit rate matches
this base rate plus a small fraction of papers where arXiv ingested the DOI from
Crossref auto-population.

Title+author works because:

- arXiv's title field is always populated (that's the primary key for the preprint).
- The author list is structured `lastname, firstname` and reliably indexed.
- arXiv's own search UI uses `ti:` and `au:` — well-trodden query path.
- Quoted-phrase matching with `ti:"<title>"` returns exact title hits, and the
  `+AND+au:<lastname>` filter eliminates same-title-different-paper false positives.

## API query shape

Working pattern:

```
https://export.arxiv.org/api/query?search_query=ti:%22<URL-encoded title>%22+AND+au:<lastname>&max_results=3
```

Notes:

- `search_query` value must keep `:` and `+` literal — URL-quote everything else.
  In Python: `urllib.parse.quote(q, safe=":+")`.
- `ti:"<title>"` is the quoted-phrase form. Without quotes, arXiv treats it as
  AND-of-words and matches any preprint containing every token in the title,
  including word-salad coincidences.
- Strip non-word punctuation from titles before quoting: `re.sub(r'[^\w\s\-]', ' ', title)`.
  Otherwise embedded `"` or `[` from the OSTI biblio HTML break the quoted-phrase
  parser.
- `au:<lastname>` is loose — first author lastname is enough; arXiv matches against
  any author. Don't try to pass the full author list, the query gets long fast.
- `max_results=3` is the right tradeoff: usually only 1 plausible hit, occasionally
  2 versions of the same preprint, very rare 3rd-party noise to filter.

## Fuzzy validation

arXiv title-search can return false positives when:

- The OSTI biblio title has a publisher-stage subtitle that the preprint lacks
  (`Foo bar: Insights from baz` vs `Foo bar`).
- Punctuation/Unicode differences (em-dash vs hyphen, Greek letter vs ASCII).
- Conference proceedings titles that share opening words with unrelated preprints.

Always validate the top hit's title against the query title with a token-overlap
test before downloading the PDF. Reference threshold: **≥70% token overlap**
(stopwords excluded). Tokens = lowercased `[a-z0-9]+` runs, minus
`{the, a, an, of, and, for, in, on, to, with, at, by, from, as}`.

```python
def tokens(s):
    return set(re.findall(r"[a-z0-9]+", s.lower())) - {"the","a","an","of","and",
        "for","in","on","to","with","at","by","from","as"}

def title_match_ok(query_title, found_title):
    qt, ft = tokens(query_title), tokens(found_title)
    if not qt: return False
    return len(qt & ft) / len(qt) >= 0.7
```

This drops the false-positive rate without losing the legitimate exact-title hits.
In the 2026-06-10 smoke, all 10 accepted hits passed this check; the 10 rejections
were genuine no-match cases (paper not on arXiv).

## Source of titles + authors

OSTI biblio HTML always includes:

```html
<meta name="citation_title" content="The actual paper title">
<meta name="citation_author" content="Lastname, F.">
<meta name="citation_author" content="Lastname2, G.">
...
```

The biblio URL is `https://www.osti.gov/biblio/<osti_id>`. Fetch it once per paper,
extract first `citation_title` and first `citation_author`, feed both to arXiv
search. The fetch is the rate-limiting step (~1s per OSTI biblio request) — wrap
the whole pipeline at 3 calls per cycle:

1. Fetch OSTI biblio HTML — 1 req
2. arXiv title+author search — 1 req
3. arXiv PDF download — 1 req

At 4s/req polite floor (each call separately), one paper takes 12s end to end. For
11K targets split 2 ways across non-ANL hosts (M1, cherryrd), ~6.3h wall per host
in parallel.

## Rate-limit discipline

arXiv enforces ~1 req/3s soft limit across **all** queries from your IP. Don't
sub-second this. The polite UA is `OSTI-corpus-recovery/1.0 (mailto:<your email>)`.

Per-host workers run single-threaded with `time.sleep(4.0)` between every request
of any kind (biblio fetch, arXiv search, arXiv PDF). Going multi-threaded saves
nothing because arXiv's rate limit is per-IP, not per-connection.

ANL IP block (cels-rbdgx*, hcdgx*, almost certainly aurora) is hard-rate-limited
to immediate 429. **Don't run this worker on ANL hosts.** Working hosts as of
2026-06-10:

- M1 (home — different IP)
- cherryrd (different IP from cels)
- prokko (UChicago CS subnet 128.135.123.x — clean for arXiv)

## Yield expectations

50% on the curated APS/IOP/AIP/AAS/MNRAS/EDP/AIP/Science/PNAS/AR pool. Lower on
non-physics labs or non-physics DOI prefixes. For pure-chemistry corpora
(ACS/RSC), expect <5% (most chemistry isn't on arXiv).

Realistic planning: **30-40% blended hit rate** across a mixed
physics-leaning DOE corpus. For 11K target rows, ~4-5K PDFs recovered.

## Anti-patterns

- **Don't query arXiv full-text search (`all:` field).** It returns too many
  false positives — even with author filter — because arXiv full-text indexes
  cited references too.
- **Don't try arXiv author search alone.** Returns hundreds of hits per common
  lastname; useless without title constraint.
- **Don't omit the title sanitization step.** Unicode quotes, math symbols,
  or HTML entities in the OSTI title will break the quoted-phrase parser
  and you'll get either empty results or 500-error responses.
- **Don't trust the top hit blindly.** Run the token-overlap validation —
  cheap, eliminates the most obvious false positives.
- **Don't multi-thread per host.** Rate limit is per-IP, not per-connection.
  Multi-thread just gets you 429ed faster.

## Lesson

When a recovery worker shows <5% hit rate on a target pool where ground truth
suggests recovery should be possible, **the query shape is wrong, not the
upstream coverage**. Check what the upstream's own search UI uses as primary
keys, and match that — don't assume the API's most obvious field (DOI) is
indexed densely.

Generalizes to: HF Hub by model name vs by author, S2 by DOI vs by title,
Crossref by member-id vs by ISSN, etc. When you suspect undercount,
re-query with a different key before declaring the upstream "doesn't have it."
