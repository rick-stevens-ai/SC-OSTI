# Email validation via citation-database triangulation

Worked example, 2026-06-09 (later same day as the SMTP-battery work).
**Companion** to `email-validation-battery-2026-06-09.md`, not a replacement.

After the SMTP battery completed and Rick flagged that web-search-based
validation was worth trying, this session built a **second-stage
validator** that confirms the `(osti_id, email, name_hint)` binding by
cross-referencing what authoritative third-party citation databases say
about the same paper.

Corpus: same 100-email random sample from the OSTI extraction (post-
multi-email-split fix). Script: `tests/run_tests_web.py`. Result: 32
seconds wall, $0 cost, **70/100 reach PROBABLE-or-stronger**
corroboration without any LLM calls.

## The four methods that work

| ID | Method | Signal extracted | Hit rate |
|----|--------|------------------|----------|
| M1 | OpenAlex `/works/doi:<doi>` -> authorships | name match + author affiliation | 82/100 |
| M2 | Crossref `/works/<doi>` -> author list | name match + author affiliation | 83/100 |
| M3 | OSTI `/api/v1/records/<id>` -> authors[] | name match + bracketed affiliation + ORCID | 45/100 |
| M4 | DOI landing page scrape (`doi.org/<doi>`) | literal email string in HTML | 15/100 |

All four are **free, no API key, no rate-limit issues at 6-way
parallelism**. Aggregate runtime ~10 req/s across all four APIs.

## The method that DOES NOT work — kill it on sight

**Web search engines (Bing / Google / DuckDuckGo) are not viable
without a paid Search API.** All three serve heavily-JS-rendered SERPs
where the actual result content is loaded client-side and isn't in the
static HTML response. Quoted-email queries (`"first.last@inst.edu"`)
get the "no results found" template regardless of whether the email
exists anywhere on the open web.

Concrete failure pattern verified this session:

- Bing static HTML returned 6 occurrences of the email for EVERY
  query — including obviously-fake `kxqzqzxnotreal12345@anl.gov`.
  All 6 occurrences were echoes of the query string in the page
  chrome (title, og:url, og:title, search-box `value=`, pagination
  `aria-label`, "more results for X" link). The `<li class="b_algo">`
  result containers were the same 10 unrelated stock links for fake
  and real queries alike.
- Google SERPs strip emails from result snippets entirely (0 hits
  for both fake and real queries; 90KB JS payload mostly).
- DuckDuckGo's HTML endpoint returns HTTP 202 "are you a robot"
  before showing anything.

**Pre-flight test** — if you're tempted to add a web-search validator
to ANY identifier-validation pipeline, first run the validator with
ONE known-good identifier and ONE obviously-fake identifier. If the
hit count is identical, the validator is broken and you need a real
Search API (Brave Search, Serper, Google Custom Search) — there is
no free-scrape workaround.

## Verdict ladder — strong-to-weak

```
CONFIRMED  email verbatim on DOI landing page (M4 hit)
STRONG     name+affiliation agreement from ALL 3 of {OSTI, OpenAlex, Crossref}
LIKELY     name+affiliation agreement from 2 of 3
PROBABLE   name+affiliation agreement from exactly 1 of 3
WEAK       name match somewhere but no affiliation agreement
UNVERIFIED no positive signal at all
```

The ladder is designed so that **STRONG+CONFIRMED is the high-trust
pool** suitable for any downstream where false-positive cost matters
(outreach, contact campaigns). PROBABLE+ (70% of the sample) is fine
for analytics and reporting. UNVERIFIED-only contacts should be
reviewed by hand or skipped — most are either group addresses
(`atlas.publications@cern.ch`) or extractions where the name field
was empty.

## Affiliation agreement — the heuristic that saved 35% of WEAK

The naive approach is a curated `DOMAIN_HINTS` table mapping every
email domain to a list of strings expected to appear in the
institution name (`{"anl.gov": ["argonne"], "lbl.gov": ["lawrence
berkeley", ...]}`). This works for the ~30 DOE labs + ~30 major
universities and **fails completely on the long tail**. First-pass
WEAK bucket was 35/100 — almost all caused by missing hint entries
for `princeton.edu`, `uchicago.edu`, `northwestern.edu`,
`stonybrook.edu`, etc.

**The fix is a 3-tier cascade in `affiliation_matches_domain()`:**

```python
def affiliation_matches_domain(domain: str, affils: list[str]) -> bool:
    if not affils:
        return False
    joined = " ".join(affils).lower()
    # 1. Curated hints (highest precision)
    for h in DOMAIN_HINTS.get(domain, []):
        if h in joined:
            return True
    # 2. Domain-prefix-as-institution
    GENERIC = {"edu","gov","org","com","net","ac","cn","uk","eu","us",
               "de","fr","it","ca","au","jp","kr","ch","es",
               "mail","physics","chem","cs","ece","math","phys"}
    parts = [p for p in domain.split(".")
             if p not in GENERIC and len(p) >= 3]
    for p in parts:
        if p in joined:         # 'princeton' in 'Princeton University'
            return True
        if p in ABBREV_EXPANSIONS and ABBREV_EXPANSIONS[p] in joined:
            return True
    return False
```

The domain-prefix heuristic catches the long tail mechanically without
hand-curation. The abbreviation table (`vt`->`virginia tech`,
`utk`->`tennessee`, `cup`->`china university of petroleum`, etc.)
handles the cases where the prefix is an acronym that doesn't appear
in the spelled-out name. Adding the 3-tier cascade **upgraded 13/35
WEAK records to PROBABLE-or-better** on the smoke set.

Bonus: the heuristic is conservative by design (substring match,
generic-parts skipped) so the false-positive rate stayed at zero
across the smoke set. The institution-name strings from OpenAlex/
Crossref/OSTI are well-formed enough that "princeton" matches
"Princeton University" but doesn't false-match anything else in
typical affiliation prose.

## Parsing OSTI's `authors[]` field

OSTI's JSON API returns each author as a single string with a
bracket-and-paren encoding:

```
"Lu, Jun [Argonne National Lab. (ANL), Lemont, IL (United States)] (ORCID:0000000308588577)"
"Chi, Xiao [South China Univ. of Technology (SCUT), Guangzhou (China); National Univ. of Singapore (Singapore)]"
"Wu, Kunze [South China Univ. of Technology (SCUT), Guangzhou (China)]"
```

A multi-affiliation author can have multiple `[...]` blocks OR a
single block with `;`-separated affils inside. Reliable parser:

```python
import re
# Name = everything before first '[' or ' (ORCID'
m_name = re.match(r"^([^\[]+?)(?:\s*\[|\s*\(ORCID|$)", author_str)
name = (m_name.group(1).strip().rstrip(",") if m_name else "")

# Affiliations = each [...] block (often just one)
affils = re.findall(r"\[([^\]]+)\]", author_str)

# ORCID = digits + optional X check digit
m_orcid = re.search(r"ORCID:\s*([\dX]+)", author_str)
orcid = m_orcid.group(1) if m_orcid else None
```

The ORCID is **valuable downstream** even when you already have the
email. It's a persistent author identifier that survives email
changes (job moves, mailbox deprecation, the corresponding-author
field rotating between PIs). M3 surfaced an ORCID for ~30% of the
sample. For any contact corpus that's going to live longer than the
emails in it, capture the ORCID alongside.

## Verdict counters — sample results

```
CONFIRMED  : 15   (email literally on DOI page; mostly open-access publishers)
STRONG     : 24   (3-source name+affil)
LIKELY     : 17   (2-source name+affil)
PROBABLE   : 14   (1-source name+affil)
WEAK       : 20   (name match only, no affil agreement)
UNVERIFIED : 10   (no signal at all)
```

UNVERIFIED breakdown (10):

- 6 records had empty `name_hint` from the extractor — fixable by
  backfilling from OSTI API's `authors[]` field (which M3 already
  fetches, so cost is zero).
- 2 still had multi-email contamination (`a@x; b@y` with `;<space>`
  separator) that the 2026-06-08 `fix_split_emails.py` splitter
  didn't catch — splitter could be extended.
- 2 were legitimate group addresses (`atlas.publications@cern.ch`,
  `auger_spokespersons@fnal.gov`). Flag these as
  `source=group_address` so downstream campaigns can skip them.

## When this method is the right call

- You have a corpus of identifiers (emails, names, ORCIDs)
  extracted from papers, and the papers have **DOIs**.
- The validity question is **does the extracted person actually
  belong on this paper at this institution**, not just **does the
  mailbox exist** (the SMTP battery already answers the second).
- You want fast, free, parallel, no-LLM corroboration that can run
  on the full corpus in a few hours.
- You're going to use the verdicts to **tier downstream work**
  (high-trust pool for outreach, low-trust pool for review).

## When this is NOT the right call

- You're validating a single record interactively — overkill, just
  read the paper or check Google Scholar by hand.
- Your identifiers don't have DOIs — both M1 and M2 require a DOI,
  and the OSTI metadata (M3) varies in author-detail quality. Limit
  to corpora where >80% of records have DOIs.
- You need to validate that an email *currently* belongs to a
  reachable mailbox — that's the SMTP battery's job, and even that's
  weakened by modern mailgates (see the companion reference's
  T3 CAVEAT).

## Performance and rate-limit shape

- 6-way ThreadPoolExecutor saturates all four APIs without any 429s
  on a 100-record run. 12 workers is probably also fine but wasn't
  tested.
- OpenAlex: ~200ms median per call, very reliable.
- Crossref: ~200ms median per call, reliable.
- OSTI JSON API: ~200ms median per call, reliable.
- DOI landing fetch: median 400ms, tail of 3-10s when the redirect
  chain goes deep into publisher walls. Set a 20s per-request timeout.
- For a 117K-email full corpus run: estimated ~5 hours at 12 workers,
  $0 cost.

## Files that should land in the repo

```
tests/run_tests_web.py        # the 4-method validator
tests/results_web.jsonl       # per-record machine output
tests/RESULTS_WEB.md          # human-readable summary
```

Add as a peer to the SMTP-battery artifacts. Both should be runnable
independently — the validation question they answer is different.
