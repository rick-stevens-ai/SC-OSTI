# Taxonomy-merge from community sources (2026-06-09)

When the question is **"what's the label space for the classifier?"** — not
"what's in this document?" — you're building a taxonomy, not extracting
fields. Same family of problem as corpus extraction (heterogeneous inputs,
authoritative sources, smoke-then-scale), different output shape.

Use this when the user says:
- "build a domain classifier" / "neutral classification scheme"
- "merge the X categories with the Y categories"
- "what taxonomies cover this corpus"
- "replace the [skewed/biased/legacy] classifier"

## The seven canonical sources for scientific-paper classification

Memorize these — most "neutral taxonomy" jobs pull from a subset of this
list:

| Source | URL | Shape | Pull method |
|---|---|---|---|
| **arXiv** | `https://arxiv.org/category_taxonomy` | ~155 leaves, 4-level (groups → archives → categories → desc) | HTML scrape, single page |
| **bioRxiv** | `https://api.biorxiv.org/details/biorxiv/<start>/<end>/<cursor>` | ~25 categories | API sample across 3-4 month-windows × 8 cursor pages |
| **medRxiv** | `https://api.biorxiv.org/details/medrxiv/<start>/<end>/<cursor>` | ~49 categories | Same shape as bioRxiv |
| **ChemRxiv** | `https://chemrxiv.org/engage/chemrxiv/public-api/v1/categories` | ~17 categories | **CLOUDFLARE-WALLED — must curate from published taxonomy** |
| **EarthArXiv** | `https://api.osf.io/v2/providers/preprints/eartharxiv/taxonomies/` | ~25 disciplines | API returns GENERIC OSF TREE (708 entries) not earth-only — curate from `eartharxiv.org/about/` |
| **engrXiv** | `https://api.osf.io/v2/providers/preprints/engrxiv/taxonomies/?per_page=100` | 114 total, 12 parent disciplines | OSF API, paginate via `next` link |
| **OSTI DOE Subject Categories** | sampled from OSTI `/api/v1/records` `subjects[]` field | 46 numbered codes (`36 MATERIALS SCIENCE`...) | **No direct endpoint** — sample 5K records across 5 years and extract the canonical numbered prefixes |

## Pull-pattern pitfalls

### arXiv: scrape, don't API

The arXiv API has no taxonomy endpoint. The category page is HTML. Working
regex (verified 2026-06-09):

```python
import re
html = open('/tmp/arxiv_tax.html').read()
leaf_re = re.compile(
    r'<h4>([a-zA-Z\-\.]+)\s*<span>\(([^)]+)\)</span></h4>'
    r'\s*</div>\s*<div class="column"><p>([^<]+)</p></div>')
leaves = leaf_re.findall(html)  # → 155 (code, name, desc) tuples
```

The naive `<h4>...</h4>` regex returns 0 hits because each leaf entry has
the code+name in the `<h4>` AND a description in a sibling `<div
class="column">`. The combined pattern catches both.

### bioRxiv/medRxiv: API sample across multiple month-windows

bioRxiv's `/details/<server>/<YYYY-MM-DD>/<YYYY-MM-DD>/<cursor>` endpoint
returns 100 results/page. Each result has a `category` string field. There's
NO endpoint that returns the category list directly.

Use 3-4 windows from different years × 8 cursor pages each = ~3K papers
sampled. That's enough to surface all ~25 categories for bioRxiv (~49 for
medRxiv) with stable counts. Smaller samples miss rare categories
(`paleontology`, `palliative medicine`).

```python
windows = [("2024-09-01", "2024-09-30"),
           ("2024-03-01", "2024-03-31"),
           ("2023-06-01", "2023-06-30"),
           ("2022-11-01", "2022-11-30")]
for s, e in windows:
    for cursor in range(0, 800, 100):
        url = f"https://api.biorxiv.org/details/{server}/{s}/{e}/{cursor}"
        # fetch + extract category field
        # break inner loop on empty result page (end of window)
```

bioRxiv DOES NOT need an API key. Headers: just `User-Agent: Mozilla/5.0`.
No rate limit observed at sequential calls.

### ChemRxiv: Cloudflare wall, curate by hand

Every endpoint variant 403s with a Cloudflare challenge page (5500-5900 bytes
of HTML starting `<!DOCTYPE html><html lang="en-US"><head><title>Just a
moment...`). Wayback Machine returns a Wayback wrapper page, not the JSON.
Crossref subject field is empty for the `10.26434` prefix.

There is no automation path. ChemRxiv has 17 stable top-level categories —
hand-curate from the published taxonomy. They don't change. Curated list
(verified 2026-06-09):

```
agriculture, analytical, biological, catalysis, chemed, chemeng,
earthspace, energy, inorganic, materials, matsci, nano, organic,
organomet, physical, polymer, theoretical
```

### EarthArXiv: OSF API returns the wrong tree

`/v2/providers/preprints/eartharxiv/taxonomies/` returns 708 entries — the
entire OSF generic subject tree (`Accessibility`, `Adult and Continuing
Education`, ...). Less than 5% are actually earth-discipline.

Filter manually from the curated 23-25 earth disciplines on
`eartharxiv.org/about/` (atmospheric, biogeoscience, climate, cryosphere,
geochem, geodesy, geomorph, hydrology, marine, mineral, ocean, paleo,
planetary, remote-sens, sediment, seismology, soil, tectonics, volcano,
plus a few meta).

### engrXiv: OSF API works, but `parents_count` field is absent

`/v2/providers/preprints/engrxiv/taxonomies/?per_page=100` paginates cleanly
and returns 114 entries. The schema doc says each entry has a `parents_count`
field for "top-level filtering" — **the field doesn't exist in practice**
(returns `None`). Use `child_count > 0` as the proxy for "this is a parent
discipline":

```python
parents = [s for s in all_subjects if (s.get('child_count') or 0) > 0]
# → 12 disciplines: Aerospace, Aviation, Biomedical, Chemical, Civil,
#   Computer, Electrical, General Engineering, Eng Sci & Materials,
#   Materials Sci & Eng, Mechanical, Operations Research
```

Use the 12 parents for taxonomy structure; keep the 114 leaves available
in `engrxiv.json["all_leaves"]` for finer-grained classification later if
needed. Most DOE applied-engineering work classifies at parent-discipline
granularity anyway.

### OSTI DOE Subject Categories: no endpoint, must sample

OSTI's documented "subject categories" pages (`/etde/cataloging-codes.jsp`,
`/subject-categories`, `/elink/241-6.pdf`, `/elink/documents/241-6.pdf`)
all return 404. The Wayback Machine has snapshots but they're the
HTML-rendered category pages, not a machine-readable list.

The right approach: sample OSTI records via the `/api/v1/records` endpoint,
extract the `subjects[]` field, and the **numbered codes surface organically
as `NN NAME` prefixed strings**. Sample 5K records across 5 years for full
coverage of all 46 codes:

```python
import re
from collections import Counter
cats = Counter()
for year in (2020, 2021, 2022, 2023, 2024):
    for page in (1, 2):
        url = f"https://www.osti.gov/api/v1/records?publication_date_start=01/01/{year}&publication_date_end=12/31/{year}&rows=500&page={page}"
        # fetch records, iterate, for s in rec.get('subjects', []): cats[s.strip()] += 1

# Isolate the canonical DOE numbered codes:
doe_codes = []
for name, n in cats.most_common():
    m = re.match(r'^(\d{2})\s+(.+)$', name)
    if m:
        doe_codes.append({"code": m.group(1), "name": m.group(2), "sample_count": n})
# → 46 unique codes when sampled across 5 years
```

The non-numbered entries in `subjects[]` are journal subject headings
(`Physics`, `Materials Science`, Web of Science style — ~600 distinct) and
free keywords (~5,000 distinct, useful for tag-cloud but not for taxonomy).
**The 46 numbered codes are the authoritative DOE taxonomy.** They appear
at the start of every legitimate DOE-deposited record's `subjects` array,
followed by free-text journal subjects.

3/5 records have a non-empty `subjects` field in the 5K sample (~60%
coverage). Records with empty `subjects` are usually journal articles that
OSTI ingested without classification.

## Merge architecture

10-supergroup × 42-discipline × ~240-leaf shape worked well as a DOE-neutral
classifier:

```
phys     Physical Sciences          (7 disc,  36 leaves)
math     Mathematics & Statistics   (3 disc,  18 leaves)
cs       Computer Science & AI      (4 disc,  38 leaves)
chem     Chemistry & Materials      (3 disc,  17 leaves)
earth    Earth, Environment, Climate(4 disc,  23 leaves)
bio      Life Sciences & Biology    (5 disc,  27 leaves)
health   Health & Medicine          (4 disc,  39 leaves)
eng      Engineering & Applied      (4 disc,  14 leaves)
energy   Energy, Fuels & DOE Mission(6 disc,  22 leaves)   ← DOE-distinctive
econ     Economics & QuantSocial    (2 disc,   5 leaves)
```

Each leaf carries `sources: [{source, code, name}]` showing every preprint-
server category or DOE code that maps in. Leaves frequently merge sources
(`phys.cmp.supercon` ← arXiv `cond-mat.supr-con` + OSTI `75`).

### Where preprint servers DON'T cover

The DOE-distinctive supergroup `energy.*` has **no preprint-server source**.
Nuclear (fuel cycle / reactors / waste / safeguards), fossil fuels, energy
policy/economy, radiation protection / dosimetry — all OSTI-only.

Engineering applied/applications (manufacturing, grid, batteries, ICS
cybersec, transport, water-energy) has engrXiv + OSTI codes but is
genuinely underserved — DOE applied-energy labs (NREL/INL/SNL/NETL/PNNL)
do not commonly deposit to engrXiv.

This is the **right** outcome: the merged taxonomy is honest about where
preprint-server ground-truth labels exist and where they don't. The
classifier confidence should reflect this.

### Source coverage stats on the 2026-06-09 merge

| Source | Mapped Into Merged | % | Notes |
|---|---|---|---|
| arXiv | 147/155 | 95% | 8 unused are cross-list aliases |
| bioRxiv | 24/25 | 96% | `scientific communication and education` is meta |
| medRxiv | 49/49 | 100% | |
| ChemRxiv | 16/17 | 94% | `earthspace` folded into `earth.*` |
| EarthArXiv | 23/25 | 92% | `data-info` + `education` meta |
| engrXiv | 12/12 parents | 100% | Sub-leaves available but engineering classifies at parent granularity |
| OSTI DOE | 46/46 | 100% | All canonical codes mapped |

239 total leaves. ~95-100% source coverage across all 7 inputs.

## Output artifacts

For each taxonomy build, produce:

1. **Per-source raw files** (`<source>.json`) — preserve the upstream
   taxonomy verbatim so the merge is traceable and re-mergeable.
2. **`merged_taxonomy.json`** — machine-readable, supergroup → discipline
   → leaves with `sources: [{source, code, name}]` on each leaf.
3. **`merged_taxonomy.md`** — human review document. Sections:
   - Sources table with URLs and pull methods
   - Architecture summary
   - Source coverage table
   - Full structure (all supergroups → disciplines → leaves)
   - **Known coverage gaps** (explicit list of leaves with no preprint source)
   - Usage notes (single-label vs multi-label vs supergroup-only modes)
   - Comparison to whatever legacy classifier this replaces

## Downstream classifier validation

Once the taxonomy exists, the natural next step is a validation set: pull
~2K papers whose DOI is in BOTH the target corpus (OSTI, etc.) AND one of
the preprint servers, use the preprint-server's category as ground-truth,
run the classifier, measure leaf-level accuracy.

Don't ship the classifier without this. Without ground-truth validation
you can't distinguish "the taxonomy fits the corpus" from "the classifier
hallucinated coherent-looking labels."

## When this pattern DOES NOT apply

- **Single-corpus, single-source classification** (e.g. just arXiv papers
  → just arXiv labels): don't merge, just use the source taxonomy directly.
- **Recruitment / intent classification** (e.g. "which paper fits which
  funding-call topic"): use the intent-skewed scheme, not a neutral one.
  These are different questions — neutral classification asks "what is
  this paper about?", recruitment classification asks "which of these
  buckets would the sponsor put it in?"
- **Small corpus (<1K papers)**: an LLM with the unified taxonomy as a
  prompt argument works fine; you don't need to build a dedicated
  classifier.
