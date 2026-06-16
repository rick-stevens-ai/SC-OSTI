# Extract → Resolve → Categorize: a third stage for corpus validation

When you've extracted structural signals (DOIs, URLs, accession numbers) from
a corpus of cards/reports/papers, the next question is almost always
**"can we actually find the underlying object?"** This reference documents the
pattern that worked on the OSTI data cards (2026-06-05).

## When to use

Trigger phrases from the user:
- "how easily can we find the [data/code/model] mentioned in these"
- "let's verify the [URLs/citations/references] actually resolve"
- "are these [datasets/repos] really public"
- "rate the [findability/accessibility/replicability] of each card"

Threshold: the corpus has structural pointers (DOIs, URLs, accession IDs) and
you want to know what fraction actually lead somewhere useful.

## The pattern in three stages

### Stage 1 — structural extraction (regex, fast)

Pull every plausible signal from each card body. For data/code cards typical
patterns are:

```python
DOI_RX = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
URL_RX = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
ACCESSION_PATTERNS = {
    "Zenodo":     re.compile(r"zenodo\.org/record/\d+", re.IGNORECASE),
    "GEO":        re.compile(r"\bGSE\d{3,7}\b"),
    "PRIDE":      re.compile(r"\bPXD\d{6}\b"),
    "SRA":        re.compile(r"\b[SED]RR\d{6,8}\b"),
    "ArrayExp":   re.compile(r"\bE-[A-Z]{4}-\d+\b"),
    "BioProject": re.compile(r"\bPRJ[EDN][A-Z]\d+\b"),
    "ChEMBL":     re.compile(r"\bCHEMBL\d+\b", re.IGNORECASE),
    "Figshare":   re.compile(r"figshare\.com/[^\s]+", re.IGNORECASE),
    "Dryad":      re.compile(r"datadryad\.org/[^\s]+", re.IGNORECASE),
}
```

**DO NOT include PDB/GenBank/UniProt naively.** Their accession formats
(`[1-9][A-Z0-9]{3}` for PDB, `[A-Z]{1,2}\d{5,8}` for GenBank, the SwissProt
6-char pattern for UniProt) are so loose they match substrings of DOIs,
journal numbers, even year strings. In the OSTI sample, my naive PDB regex
matched all 20 cards — every single one, because tokens like `1038`, `2026`,
`1021` from DOIs and dates look like PDB IDs. Either anchor on context
(`PDB:\s*`, `pdb\.org/`, `rcsb\.org/structure/`) or skip these classes for
structural extraction and lean on URLs/DOIs instead.

### Stage 2 — resolution (parallel HEAD, ~5s for ~50 URLs)

For each structural signal, build the canonical resolver URL and HEAD it:

| Signal kind | Canonical resolver URL |
|---|---|
| DOI | `https://doi.org/<doi>` (302 to publisher) |
| Zenodo | `https://zenodo.org/record/<id>` |
| GEO | `https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=<gse>` |
| PRIDE | `https://www.ebi.ac.uk/pride/archive/projects/<pxd>` |
| SRA | `https://www.ncbi.nlm.nih.gov/sra/?term=<id>` |
| BioProject | `https://www.ncbi.nlm.nih.gov/bioproject/?term=<id>` |
| ChEMBL | `https://www.ebi.ac.uk/chembl/compound_report_card/<id>/` |
| URL (raw) | the URL itself |

Use `urllib.request` with method=HEAD and a `ThreadPoolExecutor(max_workers=8)`.
Cap signals per card (e.g. top-3 DOIs and top-3 URLs) — otherwise one card
with a long citation list dominates the call budget.

**The HEAD response classification matters:**
- **2xx / 3xx**: resolved, signal is "live"
- **403**: usually publisher paywall blocking HEAD (Wiley, IEEE, AIP, MDPI,
  ACS all do this). The DOI itself is valid; the data is just gated. Mark as
  "valid-paywalled" not "broken."
- **404**: real broken link OR mangled input. Investigate.
- **Network error / timeout**: retry once then give up; mark as "unreachable."

### Stage 3 — categorize failures by mechanism

This is the high-value step Rick will actually act on. Bucket the failures:

1. **Mangled DOIs / URLs** (the extractor concatenated stuff). Telltales:
   - DOI ends in a capitalized word: `10.1038/s41467-024-55655-3Autonomous`
   - DOI has extra trailing digits: `10.1186/2049-2618-1-8` is fine,
     `10.1021/acsphotonics.8b0014630` is not (the canonical is
     `10.1021/acsphotonics.8b00146`, extractor glued the next citation number on)
   - URL ends in punctuation/word fragment: `github.com/Auto-Mech/PIPPy,` or
     `github.com/GalSim-developers/GalSimsure`
   - **Fix**: snip at first capitalized-word boundary or trailing punctuation;
     re-resolve.

2. **Publisher paywalls** (valid DOI, just gated). Tell from the User-Agent
   trick: try a GET with a browser UA — if you get HTML back instead of 403,
   the DOI is fine.

3. **Genuinely broken** (real 404 even after cleanup, real DNS failure).
   These are typos in the original paper or dead repositories. ~5-10% in
   typical corpora; record as "unrecoverable."

## Reporting shape

Two numbers the user actually cares about:

- **Raw findability**: fraction of cards where ≥1 signal resolved live without
  any cleanup (lower bound).
- **Cleaned findability**: same, but after DOI repair + GET-fallback for 403s
  (upper bound).

The gap between them measures how much downstream effort (manual repair / re-
extraction) is worth investing. On the OSTI data cards smoke (20 cards, 46
signals) the gap was **45% raw → 75-80% cleaned**, which is a strong "yes,
worth investing" signal.

## Pitfalls observed

- **My PDB regex was junk**: zero-context 4-character pattern matched every
  card. Lesson: structural extractors for short-string IDs MUST have context
  anchors, or the false-positive rate kills any downstream report.
- **doi.org never 200s on HEAD for paywalled publishers**: AIP/Wiley/IEEE
  return 403 to the redirect chain. Don't count these as failures without a
  GET-fallback retry with `User-Agent: Mozilla/...`.
- **GitHub trailing comma bug**: when DOIs/URLs appear in YAML descriptions,
  the comma between them gets eaten into the URL. Strip trailing `,;:.)>` before
  HEADing.
- **Cap signals per card** (3 DOIs + 3 URLs is plenty). Cards with citation
  lists can have 30+ DOIs, dominating runtime and obscuring the answer.
- **There's a fourth stage**: "does the URL actually contain the data" needs
  LLM judgment on the fetched page content (Zenodo records have a file list,
  GEO has metadata pages, GitHub has a file tree). That's a separate pass
  worth doing on the survivors of stage 2.

## Stage 2b — smart DOI cleanup (multi-candidate, let the resolver decide)

The biggest single-step lift on findability comes from rehabilitating mangled
DOIs before declaring them broken. The naive "snip at first capitalized-word
boundary" approach handles concatenated-word mangling but misses the trickier
reference-number-glued mangling. The pattern that works on 9/9 of the known-
mangled OSTI cases:

**Generate up to ~6 cleanup candidates per raw DOI, test each via
`HEAD doi.org/<candidate>`, first resolution wins.**

The cleanup transforms are all structural (regex); the *judgment* of "which
variant is the real DOI" is delegated to doi.org's resolution behavior, not
to regex matching. This is the same hard rule (no regex for judgment) applied
to the cleanup problem.

Cleanups to generate (in this order, most-restrictive last):

1. The raw DOI as-is.
2. Strip trailing punctuation (`).,;:]"'\\`).
3. Strip trailing concatenated alpha (handles `...55655-3Autonomous`,
   `...58327-6www.nature.com/scientificreports`, `...22028-z4` where `z4`
   is the trailing letter + digit suffix glued from a citation number).
   - Two variants of this regex catch different cases:
     `re.sub(r"(?<=\d)([A-Za-z][\w.\-/]*?)$", "", doi)` — strip alpha+anything after a digit
     `re.sub(r"(?<=\d)([A-Z][a-z][\w]*)$", "", doi)` — strip CamelCase word after digit
4. Strip a 1-3 digit "reference number" glued onto a DOI body that already
   ends in 4+ digits:
   `m = re.match(r"^(10\.\d+/[\w.\-]+\d{4,})(\d{1,3})$", doi)`
5. Generic "strip trailing 1, 2, then 3 characters" as a last resort. This
   catches `acsphotonics.8b0014630` → `8b00146` (the `30` is a citation
   reference glued on) and `1830483.183050339` → `1830503` (similar).

Then test each candidate via HEAD with a 10s timeout. The first one that
returns 2xx, 3xx, or 403 (paywall) wins. Return both the matched candidate
and how many candidates were tried — useful for debugging.

Reference implementation: `~/code/osti-cards/src/doi_resolver.py` (also at
the repo on github when pushed). Self-test on the 9 known cases passes 9/9
including:
- `10.1021/acsphotonics.8b0014630` → `10.1021/acsphotonics.8b00146` ✓
- `10.1145/1830483.183050339` → `10.1145/1830483.1830503` ✓
- `10.1038/s41598-020-58327-6www.nature.com/scientificreports...` →
  `10.1038/s41598-020-58327-6` ✓

## Stage 4 — registry validation (the "is this *really* a dataset" check)

`HEAD doi.org/<doi>` returning 200 only tells you the DOI is registered. It
does NOT tell you whether the DOI points at an actual dataset versus a
journal article that *mentions* data. This matters a lot for the typical
"how findable is the dataset" question: in the OSTI sample-200, 51% of cards
were FOUND_DOI_ONLY (DOI resolves, but to the paper not a deposit) versus
only 12% FOUND_DEPOSIT (DOI/URL points at confirmed open data).

The fix is a per-signal registry validator that queries the actual deposit
host's API:

| Signal kind | API endpoint | Returns |
|---|---|---|
| Any DOI | `https://api.datacite.org/dois/<doi>` | `type_general` field tells you Dataset / Software / Collection / Text |
| Zenodo (record id) | `https://zenodo.org/api/records/<id>` | full record: title, file list, byte size, license, access right |
| Figshare (article id) | `https://api.figshare.com/v2/articles/<id>` | file count, size, public status, type |
| PRIDE (PXD…) | `https://www.ebi.ac.uk/pride/ws/archive/v3/projects/<accession>` | title, submission date, processing protocol |
| GEO (GSE…) | `https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=<accession>` | HTML, scrape title + sample count + series type |
| SRA | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=sra&term=<acc>&retmode=json` then `esummary.fcgi?db=sra&id=<uid>` | UID + experiment XML |

**DataCite is the killer trick.** It indexes the DOI registry beyond just
Crossref — every Zenodo/Figshare/Dryad/institutional-repo DOI is in there
with a `resourceTypeGeneral` field. A Nature Comms article DOI returns
`ok: false` from DataCite (Crossref-only); a Zenodo dataset DOI returns
`type_general: Dataset`. That binary signal lets you cleanly separate
"resolves to data" from "resolves to a paper that mentions data."

Reference implementation: `~/code/osti-cards/src/registry_validators.py`
exposes `check_zenodo`, `check_pride`, `check_geo`, `check_sra`,
`check_figshare`, `check_datacite`, plus a `validate_signal(kind, value)`
dispatcher.

## Per-card verdict aggregation

After resolution + registry validation, bucket each card into one of four
buckets based on its strongest signal:

- **FOUND_DEPOSIT** — at least one signal hit a real deposit (Zenodo, PRIDE,
  GEO, SRA, Figshare, or DataCite-typed Dataset/Software/Collection)
- **FOUND_DOI_ONLY** — at least one DOI resolved but no registry classified
  it as data
- **BROKEN_SIGNALS** — has identifiers but none resolved
- **NO_SIGNALS** — no DOIs/URLs/accessions extractable at all

The "NO_SIGNALS" bucket is often a real finding, not a tool failure. In the
OSTI sample-200, 33% of cards had no extractable identifiers because they
describe instrument measurement campaigns (synchrotron beamlines, APS) where
the data lives on facility file servers and was never deposited publicly.
Don't blame the extractor for these; report them honestly as "data not
publicly deposited."

## Reporting shape (refined)

Two-table format the user finds actionable:

```
Verdict          Count   %
FOUND_DEPOSIT      24   12.0%
FOUND_DOI_ONLY    102   51.0%
BROKEN_SIGNALS      8    4.0%
NO_SIGNALS         66   33.0%
```

Plus a deposit-type breakdown:

```
Strong deposit types found:
  DataCite:Dataset       19
  SRA                     5
  DataCite:Software       4
  GEO                     3
  PRIDE                   2
  Zenodo                  2
```

And signal-level rate: "N of M extracted signals successfully resolved (X%)."

## Worked example 2 (OSTI data cards sample-200, 2026-06-05)

Same corpus as the sample-20 example below, scaled to 200 cards (every 23rd
file). Full pipeline runtime: 32.7s wall, 6.12 cards/s, 6× card-parallel ×
8× signal-parallel against live APIs.

Results:
- 408 signals extracted across 200 cards (2.04 avg per card)
- 309 resolved (75.7% signal-level)
- 36 strong deposits across 7 registry types
- 12% FOUND_DEPOSIT, 51% FOUND_DOI_ONLY, 4% BROKEN_SIGNALS, 33% NO_SIGNALS
- Findability ceiling: 63%

Tooling lives at `~/code/osti-cards/`:
- `src/pipeline.py` — end-to-end runner
- `src/doi_resolver.py` — smart cleanup (stage 2b)
- `src/registry_validators.py` — registry API checks (stage 4)
- `src/parse_cards.py` — structural extraction (stage 1)
- `samples/sample200_results.jsonl` — full output
- `reports/sample200_findability.md` — narrative report

A copy of the full pipeline template is in
`scripts/cards_findability_pipeline.py` of this skill so future sessions can
adapt it without rebuilding from scratch.

## Variant: code / model / agent cards (different signal set)

The same extract→resolve→categorize pattern works for non-data cards, but the
"strong signals" change because the question changes:

| Card kind | The question is... | Strong signals |
|---|---|---|
| Data card | Can I find/download the dataset? | Zenodo/PRIDE/GEO/SRA/Figshare/DataCite-Dataset |
| Model card | Can I find/download model weights + training code? | GitHub, GitLab, HuggingFace Hub, DataCite-Software/Model |
| Agent card | Can I find/run the agent? | GitHub, GitLab, framework+endpoint, arXiv (paper as fallback) |

Verdict states differ accordingly. For code/model/agent cards:

- **FOUND_RUNNABLE** — ≥1 resolvable GitHub / GitLab / HuggingFace repo
- **FOUND_PAPER_ONLY** — arXiv hit or DataCite Software/Dataset, but no live code repo
- **FOUND_DOI_ONLY** — a DOI resolves but registry didn't classify it as code/data
- **WEAK_MATCH_ONLY** — only an HF-search name-match (see WEAK_MATCH pitfall below)
- **BROKEN_SIGNALS** — has identifiers but none resolve
- **NO_SIGNALS** — no machine-extractable identifiers

Additional validators needed for the code-side question:

| Signal kind | API endpoint | Returns |
|---|---|---|
| HuggingFace (`org/name`) | `https://huggingface.co/api/models/<repo_id>` | model_id, pipeline_tag, downloads, likes, gated, n_siblings |
| HF name search (fallback) | `https://huggingface.co/api/models?search=<name>&limit=N` | top hits — TREAT AS WEAK_MATCH (see pitfall) |
| GitHub (`org/repo`) | `https://api.github.com/repos/<slug>` | stars, forks, license, pushed_at, archived, language |
| GitLab (`group/proj`) | `https://gitlab.com/api/v4/projects/<urlencoded-slug>` | name, star_count, visibility |
| arXiv (`YYMM.NNNNN`) | `https://export.arxiv.org/api/query?id_list=<id>` | atom feed — sniff for `<entry>`, extract title |

Reference implementation: `scripts/cards_findability_code_model_pipeline.py` in
this skill. Single script handles both model and agent card kinds (parametrized
on the first CLI arg).

### Worked example: OSTI model + agent cards (2026-06-05)

| Card kind | Sample | FOUND_RUNNABLE | Any signal | No signals | Rate |
|---|---|---|---|---|---|
| Model | 200 of 1,231 | 13.5% | 70% | 17.5% | 5.5/s |
| Agent | 86 of 86 (full) | **18.6%** | 48% | 38% | 10/s |

Best runnable hits:
- Models: `usnistgov/jarvis` (387★), `MolSSI/QCElemental` (193★), `uw-cmg/MAST-ML` (128★)
- Agents: `NervanaSystems/neon` (3,865★), `luigibonati/mlcolvar` (138★), `Libensemble/libensemble` (76★)

Pattern observations across all three card classes:

1. **Agents have the highest runnable rate.** Code-first culture; authors
   publish to GitHub by default.
2. **Agents also have the worst no-signal rate (38%).** When agent code isn't
   published it leaves even fewer breadcrumbs than experimental data — no
   instrument file path, no accession number, just prose.
3. **Models have the most signal density** (avg 3.2 signals/card vs 2.0 for data
   and 1.4 for agents). Multiple things to reference: code, training data,
   framework, benchmark — so even when the primary fails, fallbacks help.
4. **DataCite resourceTypeGeneral filtering matters across all three.** Most
   resolved DOIs are Crossref journal articles, not Datasets/Software. That's
   the line between FOUND_DOI_ONLY and a real deposit.

## CRITICAL pitfall — HF name-search is a FALLBACK, not a structural signal

When a model card has no HuggingFace URL but does have a `model_name`, an
obvious thing to do is search HF Hub for that name. **Do it, but flag the
hits weak_match=True and exclude HF-search from the n_signals counter in
your verdict math.** Two reasons:

1. **False positives are rampant.** Searching "fDETECT" returns
   `sebasatarama/F-DetectorModel` — totally unrelated. Generic multi-word
   scientific names ("Random Forest", "Kernel Ridge Regression for...") either
   match dozens of irrelevant repos or one wildly wrong one. A search hit is
   NOT a confirmed identification.

2. **A fallback MISS does not mean BROKEN_SIGNALS.** First model-card pass
   marked 30% of cards BROKEN_SIGNALS, mostly because HF-search returned zero
   hits for niche science models. That's not "broken signals" — that's "we
   tried a fallback and confirmed nothing exists." Counting fallback misses
   against the card pollutes the report. After the fix:

   ```python
   # In verdict():
   real_signals = [v for v in validations if v["kind"] != "HF-search"]
   n_sigs = len(real_signals)
   n_ok = sum(1 for v in real_signals if v.get("ok"))
   n_weak = sum(1 for v in validations if v.get("weak_match"))
   ```

   BROKEN_SIGNALS rate dropped from 30% to 12.5% — and that 12.5% is now
   actually meaningful (cards with structural signals that genuinely don't
   resolve).

The general principle: **separate "structural signal that the card claims is
real" from "discovery attempt we made to fill a gap."** Only the former should
factor into BROKEN_SIGNALS. The latter should produce its own bucket
(WEAK_MATCH_ONLY) so the user can see how much the gap-fill bought you
without distorting the no-signal rate.

## arXiv counts as a strong fallback signal (FOUND_PAPER_ONLY)

For code/model/agent cards, an arXiv hit doesn't give you runnable code, but
it confirms the paper is real and reachable. Bucket it as FOUND_PAPER_ONLY
(distinct from FOUND_DOI_ONLY where the DOI just happens to be a journal
article). The distinction matters because arXiv → paper PDF → references →
sometimes-still-recoverable code is a meaningfully different path than
"this DOI resolves to a paywalled journal article."

For data cards, arXiv is less useful as a strong signal because data cards
rarely cite preprints — they cite deposit DOIs or paper DOIs directly.

## Worked example (OSTI data cards sample-20, 2026-06-05)

Corpus: `~/Dropbox/ARGONNE-PAPERS/GOOD/NONEMPTY-DATA-CARDS/` (4628 cards
total, 20 sampled). Each card is a YAML block inside a `.txt` file with
`dataset_name`, `provider.organization`, free-form description.

Tooling: `~/code/cards-project/parse_cards.py` (stage 1) +
`~/code/cards-project/resolve_signals.py` (stage 2). Both written in pure
stdlib Python, no dependencies.

Results:
- 20/20 cards had `dataset_name` (extraction itself was clean)
- 17/20 had ≥1 URL to a known data-hosting domain
- 15/20 had ≥1 DOI
- **9/20 had ≥1 live-resolving signal on first HEAD** (45% raw)
- Failure breakdown: 12 mangled DOIs, 8 publisher paywalls, 4 real 404s
- After mangling repair: 75-80% expected cleaned findability

The 4628-card full pass would take ~30 minutes at 8 workers given the smoke
rate (~10 resolutions/s sustained). Cheap enough to just run.
