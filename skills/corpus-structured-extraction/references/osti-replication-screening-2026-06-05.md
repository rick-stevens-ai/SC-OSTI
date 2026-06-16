# OSTI replication-candidate screening (2026-06-05)

Worked example: filter ~67k DOE OSTI PDFs at `/Volumes/Cherry6TB/osti_fulltext/`
for papers replicable without a physical laboratory (pure theory, simulation,
computational analysis, informatics, ML). Target: 1000 candidates.

This is the **metadata-only screening** variant of `corpus-structured-extraction`
— we never crack open the PDFs. Title + abstract + DOE subject codes from the
OSTI API are enough signal for the LLM to make a clean three-way judgment.

## Why this pattern matters

Most "filter N thousand documents for property X" tasks have a free metadata
oracle (CrossRef, arXiv API, OSTI API, OpenAlex, Semantic Scholar) that beats
PDF text extraction on every axis: faster, structured, includes abstract +
domain classification codes, no OCR. **Always check for an API before
OCR-ing a corpus.**

## OSTI API quick reference

Endpoint: `https://www.osti.gov/api/v1/records/<OSTI_ID>` (no auth, no key).

Returns a JSON array (single element) with:
- `title`, `description` (= abstract), `subjects` (list — DOE subject-code
  taxonomy, e.g. `"71 CLASSICAL AND QUANTUM MECHANICS, GENERAL PHYSICS"`)
- `product_type` (`"Journal Article"`, `"Technical Report"`, etc.)
- `doi`, `journal_name`, `authors`, `research_orgs`, `publication_date`
- `links[]` with `rel: fulltext` (PDF URL) and `rel: citation`

Coverage in the sampled 200-PDF smoke (random across 2016-2025):
- 99% success rate (2 IDs returned empty/404)
- 100% had abstracts
- 97% had DOE subject codes
- 100% were journal articles in the sample (the OSTI fulltext mirror skews
  journal-heavy)

## Rate-limit signature

**4 workers is the sweet spot.** 12 workers triggered `http.client.RemoteDisconnected`
from the server — OSTI slammed half the connections. At 4 workers we got
**5 req/s sustained, 99% success, zero retries needed**. Full 67k corpus
extrapolates to ~3.7 hours wall time.

Don't try to be clever with worker count. If you need it faster, run two
parallel processes against disjoint year ranges (different connection pools).

## The judgment prompt that worked

Three-way verdict (REPLICABLE_NO_LAB / NEEDS_LAB / UNCLEAR) with a one-line
rationale. Tight system prompt with **explicit examples of each class** —
this is what made the LLM precise:

```
A paper is REPLICABLE_NO_LAB if its core scientific contribution can be
reproduced using only:
- theoretical/analytical derivation
- numerical simulation
- computational analysis of existing public data
- machine learning / data mining / bioinformatics / cheminformatics
- statistical reanalysis

A paper is NEEDS_LAB if reproducing the core claim requires:
- synthesizing or fabricating new physical samples
- running new experiments on beamlines, accelerators, reactors, microscopes
- collecting new wet-lab data, animal/cell studies, field measurements

A paper is UNCLEAR if the abstract is too brief or ambiguous.

A paper that ONLY analyzes already-published experimental data computationally
IS REPLICABLE_NO_LAB.
A paper that proposes a method validated against new experiments is NEEDS_LAB.

Respond with EXACTLY this format on two lines:
VERDICT: <REPLICABLE_NO_LAB | NEEDS_LAB | UNCLEAR>
WHY: <one short sentence>
```

The "ONLY analyzes existing data IS REPLICABLE_NO_LAB" disambiguation line
is what makes the model treat reanalysis papers correctly. Without it,
papers that mention experimental data get marked NEEDS_LAB by mistake.

## Smoke results (50-paper LLM judgment pass)

- **40% REPLICABLE_NO_LAB** (20/50) — extrapolates to ~27k from 67k corpus
- 50% NEEDS_LAB (25/50)
- 10% UNCLEAR (5/50, mostly genuinely under-spec'd: workshop reviews, missing abstracts)

The model's rationales were surgical. Sample REPLICABLE picks:
> "uses existing DELVE DR1 and Gaia EDR3 data, no new observations"
> "tests Community Land Model against existing AmeriFlux data"

Sample NEEDS_LAB picks:
> "sub-barrier Coulomb excitation requiring accelerator/beamline"
> "enzyme kinetics assays, isotope labeling, X-ray crystallography"

At 6 workers, judgment ran at 1.9/s, ~5-10 hours for the full 67k.

## 5-judge bake-off (2026-06-05, 50 abstracts)

Before scaling to 67k inference calls, we ran the SAME 50-abstract smoke
through all five candidate judges. Results:

| Judge          | REPL | LAB | UNCL | avg_s | agree vs Sonnet |
|----------------|-----:|----:|-----:|------:|----------------:|
| argo-sonnet46  |   19 |  27 |    4 |  2.65 | (baseline)      |
| cels-llama70   |   17 |  27 |    6 |  0.93 | **100%** (43/43)|
| cels-gemma4    |   20 |  28 |    2 |  1.39 | 96% (44/46)     |
| cels-kimi      |    1 |   0 |   49 |  8.06 | n/a (abstains)  |
| cels-oss120    |    1 |   0 |   49 |  7.35 | n/a (abstains)  |

**Decision: use llama70 as primary production judge.** Identical agreement
with Sonnet 4.6 baseline on the binary REPLICABLE-vs-NEEDS-LAB call, ~3×
faster, free. gemma4 as tiebreaker on UNCLEAR cases (96% concordance, slightly
more decisive — only 2 UNCLEAR vs llama70's 6).

**kimi-k2.6 and oss120 failed the task structurally** — both abstained on
49/50 even at max_tokens=1200. This is the second project where reasoning
models have shown this exact pattern; the first was AAAR Equation Inference
in May 2026 where oss120 returned UNCLEAR on ~50% of single-letter extractions.
Lesson encoded in SKILL.md: reasoning models for chain-of-thought tasks,
instruction-tuned models (llama70, gemma4, Sonnet) for classification.

Llama70 rationales on the 6 UNCLEAR cases were legitimate ambiguity, not
laziness — workshop summaries with no specific contribution, missing abstracts,
historical accounts that don't make a testable claim.

Bake-off methodology now lives in `scripts/judge_bakeoff.py` (parent skill).
Reuse for any "which judge should I run at scale?" decision.

## Production cost projection (post bake-off)

With llama70 at 4.2 req/s on 4 workers:
- 67k judgments / 4.2 = **~4.4 hours wall time** (vs ~12.7 hours for Sonnet)
- Probably ~2 hours at 10 workers (CELS endpoint handled the bake-off load fine)
- Zero metered spend (CELS is free)

## Plaintext extraction strategy (decision parked 2026-06-05)

The corpus is **PDFs only — no plaintext sidecars exist** on `/Volumes/Cherry6TB/`.
Three options when the LLM judgment field needs body-level signal (code/data
URL mentions, methods specificity, supplementary references):

| Strategy | What | Time | When to choose |
|---|---|---|---|
| **A: metadata only** | Title + abstract + subject codes via API | 0 extra | Property X is judgeable from abstract |
| **B: hybrid (RECOMMENDED)** | Triage on metadata → extract text on top ~3k survivors only | 30min-1hr | Need body signal but want to avoid OCR'ing rejects |
| **C: extract everything** | Pre-extract all 67k PDFs to plaintext | 3-5hr parallel, 10-20 GB output | Multiple downstream tasks need plaintext |

For born-digital OSTI PDFs (modern, not scans), **PyMuPDF** is the right tool:
~1-2 sec/file, handles ~95% of modern PDFs cleanly. Fall back to **marker-pdf**
only for PDFs where PyMuPDF returns empty/garbled text (heavily equation-laden
papers, complex tables). The `ocr-and-documents` skill has both.

Default to **B** unless you know upfront that you have multiple downstream tasks
that need plaintext on the full corpus.

## DOE subject code prefix cheat sheet

Useful for prefilter sanity checks (NOT for final classification — judgment
must stay with the LLM per the HARD RULE). Top prefixes in random OSTI
fulltext sample:

| Prefix | Domain | Typical comp/lab ratio |
|---|---|---|
| 36 | Materials Science | mostly NEEDS_LAB |
| 37 | Inorg/Org/Phys Chem | mostly NEEDS_LAB |
| 42 | Engineering | mixed |
| 46 | Instrumentation/Nuclear | mostly NEEDS_LAB |
| 54 | Environmental Sciences | mostly REPLICABLE (climate models) |
| 58 | Geosciences | mostly REPLICABLE (modeling) |
| 59 | Basic Biology/Medicine | mostly NEEDS_LAB |
| 70 | Plasma Physics | mixed (simulation-heavy) |
| 71 | Classical/Quantum Mechanics | mostly REPLICABLE |
| 72 | Particle Physics | mixed (theory subset) |
| 73 | Nuclear Physics | mostly NEEDS_LAB |
| 75 | Condensed Matter | mixed |
| 77 | Nanoscience | mostly NEEDS_LAB |
| 79 | Astronomy/Astrophysics | mostly REPLICABLE (public data) |
| 97 | Math/Computing/Info Sci | mostly REPLICABLE |

Don't use this for filtering — use it for sanity-checking the LLM's
distribution after the fact. If 97 (math/computing) papers come back
NEEDS_LAB > 30% of the time, your prompt is wrong.

## Resumable JSONL pattern (generalize this)

Both `fetch_metadata.py` and `llm_judge.py` use the same resume pattern.
Crucial when a 4-hour job inevitably gets interrupted:

```python
done = set()
out_path = Path(args.output)
if out_path.exists():
    for line in out_path.open():
        try:
            done.add(json.loads(line)["osti_id"])
        except Exception:
            pass  # tolerate partial last line from interrupted write

todo = [row for row in input_rows if row["osti_id"] not in done]
out_f = out_path.open("a")  # append, not overwrite
# ... write one JSON line per record, flush every N
```

Two design rules:
1. **Append-only output, one JSON object per line.** Never rewrite the file.
2. **Tolerate a corrupt last line.** If the script died mid-write, the final
   line might be partial. The `try/except` in the resume loop drops it
   silently; the next run will redo that one record (idempotent).

## Network-fetch exception handling

`urllib.request.urlopen` can raise things that aren't in `urllib.error`:
- `http.client.RemoteDisconnected` (server closed connection, common under load)
- `ConnectionResetError`
- `socket.timeout`
- `ssl.SSLError`

A narrow `except (HTTPError, URLError, ValueError, TimeoutError)` will let
the others escape and kill threads silently inside a ThreadPoolExecutor — no
output ever materializes. **Use broad `except Exception` for network fetch
loops** and retry with exponential backoff. Catch is intentional; mark the
noqa explicitly:

```python
except Exception as e:  # noqa: BLE001 — broad catch intentional for network
    if attempt < max_retries - 1:
        time.sleep(2 ** attempt)
        continue
    return {..., "ok": False, "error": f"{type(e).__name__}: {e}"}
```

The type-name in the error string is the only way to debug what actually went
wrong after the run completes.

## Files produced

- `~/code/osti-replication-candidates/fetch_metadata.py` — parallel OSTI API
  fetcher, resumable, broad-except + 3-retry backoff
- `~/code/osti-replication-candidates/llm_judge.py` — Argo Sonnet 4.6
  three-way verdict judge, resumable
- `~/code/osti-replication-candidates/all_ids.tsv` — master index of 67,119
  PDFs (year, osti_id, path) for the Cherry6TB corpus
- `~/code/osti-replication-candidates/sample200_meta.jsonl` — metadata smoke
- `~/code/osti-replication-candidates/sample50_verdicts.jsonl` — judgment smoke

## When to reuse this recipe

Any "screen N thousand papers/reports for property X" task where:
- The corpus has stable IDs and a free metadata API exists (OSTI, arXiv,
  CrossRef, OpenAlex, Semantic Scholar, ADS for astro)
- The property X can be judged from abstract alone (replicability,
  topic match, methodology type, dataset usage)
- N is large enough that PDF OCR is impractical (>5k docs)

The two scripts in `~/code/osti-replication-candidates/` are essentially
templates — swap the API URL in `fetch_metadata.py` and the prompt in
`llm_judge.py` and you have a screening pipeline for a different corpus.
