# Corpus inventory before throughput design — worked failure 2026-06-13

## What happened

Mid-session, deep into designing a Polaris OCR pipeline:
- Wrote `~/Dropbox/XFER/plans/polaris-throughput-pack.md` with the headline "160K PDFs at ~9 node-hours via 10-job backfill fan-out."
- Wrote `marker_prod.pbs`, `nougat_prod.pbs`, `math_density_scan.py`, full SQLite work-queue, smoke→pilot→prod ladder, two-tier Marker+Nougat plan.
- Pushed everything to Polaris staging at `/eagle/projects/AuroraGPT/stevens/osti_marker/`.

Rick then asked: **"Are you convinced that the 160K PDFs do not contain duplicates"**

I wasn't. First time I'd actually counted. Real numbers:

| Source dir on Cherry6TB | PDFs | Years covered |
|---|---|---|
| `osti_fulltext/` | 67,590 (470 dup) → 67,120 unique | 2016-2025 |
| `osti_fulltext_unpay/` | 24,427 | **2006-2026** |
| `osti_fulltext_v2/` | 10,726 (still landing via cels rsync) | mixed 2018-2020 + flat IDs |
| `osti_fulltext_v2_md/` | 0 PDFs (markdown output only) | — |
| **Real total** | **~99K unique (still landing)** | **2006-2026** |

The "160K" headline was a stale pre-stub estimate from an earlier session. The corpus I was about to pipeline was smaller than I claimed in one dimension and broader than I claimed in another (decade span vs the decade I'd inventoried).

Then Rick asked: **"how many PDFs we have for the years 2006-2015"**

Answer was zero in `osti_fulltext/` (because that corpus starts at 2016) — but **7,906 PDFs in `osti_fulltext_unpay/`** that I'd never enumerated. I had walked one of four sibling source directories on the same volume.

Then Rick: **"you have downloaded new papers from various machines and you should have been moving those to Cherry6TB"**

That surfaced the missing piece. The cels rsync sweep (`proc_0699e7623e1e`, still running) had been landing `osti_fulltext_v2/` for hours. I hadn't checked.

## What the structural duplication looked like

`osti_fulltext/2016/` had two layouts merged in:
```
/Volumes/Cherry6TB/osti_fulltext/2016/<id>.pdf       (471 files, flat)
/Volumes/Cherry6TB/osti_fulltext/2016/2016/<id>.pdf  (5450 files, nested)
```

470 of the flat-layout files were byte-identical mirrors of the nested ones. 2017-2025 only had the nested form. Verification path (cheap):

```python
from collections import defaultdict
import os, hashlib

paths = open("/tmp/all_osti_pdfs.txt").read().strip().split("\n")
by_name = defaultdict(list)
for p in paths:
    by_name[p.rsplit("/", 1)[-1]].append(p)
dups = {n: ps for n, ps in by_name.items() if len(ps) > 1}

# Confirm byte-identity on a sample
for n, ps in list(dups.items())[:20]:
    sizes = [os.path.getsize(p) for p in ps]
    if len(set(sizes)) > 1:
        print(f"SIZE DIFF {n}: {sizes}")
        continue
    hashes = [hashlib.sha256(open(p, "rb").read()).hexdigest()[:16] for p in ps]
    if len(set(hashes)) != 1:
        print(f"HASH DIFF {n}: {hashes}")
# 20/20 byte-identical → safe to treat as pure duplicates
```

## Diagnostic process that would have caught this in 2 minutes pre-design

```bash
# 1. Enumerate sibling staging dirs
ls /Volumes/Cherry6TB/ | grep -iE "(osti|paper|pdf|fetch|recover)"

# 2. Per-dir count + year breakdown
for d in osti_fulltext osti_fulltext_unpay osti_fulltext_v2 osti_fulltext_v2_md; do
  [ -d "/Volumes/Cherry6TB/$d" ] || continue
  total=$(find /Volumes/Cherry6TB/$d -name '*.pdf' -type f 2>/dev/null | wc -l | tr -d ' ')
  echo "$d: $total"
done

# 3. Per-year coverage check (catches missing pre-2016 if you assume one corpus is comprehensive)
for d in /Volumes/Cherry6TB/osti_*; do
  echo "=== $d ==="
  for y in 2006 2007 2008 2009 2010 2011 2012 2013 2014 2015 2016 \
           2017 2018 2019 2020 2021 2022 2023 2024 2025 2026; do
    n=$(find "$d/$y" -name '*.pdf' -type f 2>/dev/null | wc -l | tr -d ' ')
    [ "$n" -gt 0 ] && echo "  $y: $n"
  done
done

# 4. Mixed-layout check (catches flat-vs-nested dup pattern)
for d in /Volumes/Cherry6TB/osti_*/2*/; do
  flat=$(find "$d" -maxdepth 1 -name '*.pdf' 2>/dev/null | wc -l | tr -d ' ')
  nested=$(find "$d" -maxdepth 2 -mindepth 2 -name '*.pdf' 2>/dev/null | wc -l | tr -d ' ')
  if [ "$flat" -gt 0 ] && [ "$nested" -gt 0 ]; then
    echo "MIXED $d  flat=$flat  nested=$nested"
  fi
done

# 5. In-flight processes (catches the "still landing" trap)
ps -ef | grep -E "(rsync|fetch|osti)" | grep -v grep
```

Total wall time: <5 minutes. Cost of skipping: had to redesign the manifest layer to ingest from 4 dirs + cross-corpus dedup, plus a credibility hit on the headline number.

## Generalization

This is a **pre-flight gap**, not a one-off failure. The rule:

**Before ANY throughput / packing / walltime / queue-mix proposal that takes a corpus size as input, run the 5-step pre-flight above and report the actual numbers in the same message that proposes the design.** If you find yourself typing "we have N items to process" without having `find | wc -l`'d in the last 10 minutes, stop and verify.

Same trap will land on any large-corpus pipeline design:
- Embedding/indexing runs ("we have N docs to embed")
- Fine-tuning data prep ("we have N transcripts")
- Re-extraction / re-classification ("we have N papers to score")
- Mass-format conversion (.pdf → .md, .mmd, .txt)
- Bulk recovery / fetch ("we have N gap items to backfill")

The discipline applies regardless of compute target (Polaris, Aurora, uicgpu, local). What changes is the pre-flight host (m1 vs the host where the corpus lives).

## See also

- `scripts/inventory_corpus.sh` — packaged version of steps 1-5 for the OSTI Cherry6TB shape.
- `corpus-structured-extraction` skill — full pipeline patterns once the inventory is correct.
- `dropbox-file-recovery` skill — when inventory shows 0-byte stubs and the real bytes are on another host.
