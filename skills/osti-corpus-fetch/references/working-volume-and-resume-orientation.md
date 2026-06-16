# Working volume + pre-resume orientation (2026-06-16)

This file supersedes any volume-path mention in the parent SKILL.md when they conflict. SKILL.md is at the 100K hard cap so volume migrations land here.

## Current canonical working volume: `/Volumes/SG-1-8TB`

As of 2026-06-16 Rick switched the working root for the OSTI paper project from `/Volumes/Cherry6TB` → `/Volumes/SG-1-8TB`. Cherry6TB is scheduled for erase + reformat the same day. **Do not write to `/Volumes/Cherry6TB/...` paths.** Treat it as read-only-for-rescue until confirmed reformatted.

When patching scripts, runner configs, manifests, or any file that hardcodes a `/Volumes/...` path, the rewrite target is `/Volumes/SG-1-8TB`. Future migrations: update this file's first line, don't try to patch SKILL.md.

### Top-level layout on SG-1-8TB (verified 2026-06-16)

```
/Volumes/SG-1-8TB/
├── osti_corpus/              # canonical post-consolidation tree (600G, ~200k files)
│   ├── _state/
│   │   ├── catalog.sqlite    # LIVE 1.52GB consolidated metadata DB
│   │   └── DESIGN.md
│   ├── _manifests/
│   │   └── ocr_pack_<ts>.jsonl    # Polaris/Aurora OCR staging manifests
│   ├── _audit/inventory.sqlite    # 42MB audit DB
│   ├── _stage_flat/          # flat staging area (~100k PDFs pre-promotion)
│   ├── pdfs/<year>/<id>.pdf  # canonical year-bucketed layout
│   ├── probes/               # diagnostic-probe outputs
│   └── logs/                 # runner logs
├── osti_fulltext/            # legacy v1 source (387G, 68k files)
├── osti_fulltext_v2/         # legacy v2 source (212G, 69k files)
├── osti_fulltext_unpay/      # Unpaywall fallback source (59G, 31k files)
├── osti_fulltext_v2_md/      # extracted markdown (486M, 8k files)
├── osti_recovery_2026-06-09/ # one-shot recovery batch
├── osti_probe/               # probe staging
├── Dropbox/                  # 167G mirror (snapshot, NOT a live sync target — confirm before relying)
├── BV-BRC-cites/             # adjacent BV-BRC citation work (8.2G)
├── Ozan_PARSED_PDFS/         # Ozan's parsed-PDF set (1.0G)
└── argonium_mcqa/            # MCQA dataset (124M)
```

### 0-byte stub trap at corpus root

`osti_corpus/catalog.sqlite` and `osti_corpus/state.db` at the **root** are 0-byte stubs/aliases. The real catalog DB is at `osti_corpus/_state/catalog.sqlite` (1.52 GB). Don't conclude the DB was wiped without checking `_state/` first. This matches Rick's standing "0-byte file in a Dropbox dir doesn't mean lost" pattern but applies to non-Dropbox volumes too — always check sibling/subdir for the real file before declaring loss.

## Pre-resume orientation procedure (long-pause project pickup)

When resuming a multi-stage project after >24h pause (volume migration, infra wedge, deliberate stop, etc.), do this BEFORE proposing any action. Goal: produce a single sitrep paragraph + gated plan that Rick can approve, not a "let me try things and see what happens" sequence.

### Step 1: enumerate the working volume's top level

```python
import os
root = "/Volumes/SG-1-8TB"
for e in sorted(os.listdir(root)):
    if e.startswith("."): continue
    full = os.path.join(root, e)
    if os.path.isdir(full):
        try: cnt = len(os.listdir(full))
        except: cnt = "?"
        print(f"DIR  {e:35s} ({cnt} entries)")
    else:
        print(f"FILE {e:35s} {os.path.getsize(full):>12d}")
```

Use `execute_code` Python over shell `ls` — sidesteps any HFS catalog wedge on stressed external volumes.

### Step 2: find the live state DB

Check both root and `_state/` subdir. Filter for `>1MB` files matching `*.sqlite` / `*.db`. Open read-only-immutable, list tables + row counts:

```python
import sqlite3
con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"{t:35s} {n:>10,}")
```

The standard catalog has: `papers`, `file_instances`, `decisions`, `pdf_fetch_log`, `recovery_log`, `recovery_queue`, `refresh_runs`, `build_canonical_log`.

### Step 3: pull coverage + gap snapshot

```sql
SELECT p.year, COUNT(*) AS papers,
       SUM(CASE WHEN EXISTS (SELECT 1 FROM file_instances fi WHERE fi.osti_id=p.osti_id) THEN 1 ELSE 0 END) AS w_file
FROM papers p WHERE p.year IS NOT NULL GROUP BY p.year ORDER BY p.year;
```

This tells you (a) total coverage %, (b) which year ranges are the dominant gap. Concentration tells you where to focus the next backfill pass.

### Step 4: check what was last running

`SELECT * FROM refresh_runs ORDER BY run_id DESC LIMIT 10` — gives you the last 10 runs with timestamps, types (backfill_purl, recovery_doi, exclusion_pass, etc.), and outcomes. Pair with `ls -lt logs/ | head -20` to see which logs to scan.

### Step 5: check recovery_queue state

`SELECT status, COUNT(*) FROM recovery_queue GROUP BY status ORDER BY 2 DESC` — tells you pending vs exhausted vs failed_no_doi vs recovered. Pending = cheap-resume work.

### Step 6: check the OCR manifest if downstream OCR is part of the project

`ls -lt _manifests/` — find the most recent `ocr_pack_*.jsonl` + `.summary.md`. Cat the summary. CRITICAL: open the first 3 entries of the jsonl and check whether `canonical_path` / `source_path` point at the current working volume. If they point at an old volume (e.g. `/Volumes/Cherry6TB/...` after a migration to SG-1-8TB), the manifest needs rewrite before any downstream staging pulls — see "Manifest path-rewrite gotcha" below.

### Step 7: produce the sitrep + gated plan

Format the sitrep as: working-volume name, DB live-state (size + mtime), total papers + coverage %, dominant gap (year range), recovery_queue state, last run + when it stopped, OCR manifest readiness. Then propose 3–5 stages with one-line "why" each, each gated. Ask Rick the 1–2 questions that need answering before stage 1.

Do NOT launch anything before sitrep + approval. Pre-resume orientation is itself a Rick-approved gate.

## Manifest path-rewrite gotcha

Whenever a working volume migrates, the in-flight OCR manifest (and any other artifact with absolute paths baked in — config files, MANIFEST.json, runner shell scripts, cron entries) needs a path rewrite before downstream consumers pull from it.

Procedure:
1. **Snapshot the manifest first** — copy `ocr_pack_<ts>.jsonl` → `ocr_pack_<ts>.jsonl.preflip-bak` before any edit.
2. **Rewrite paths** with stream-edit, not in-place sed (sed -i on macOS has the empty-arg trap). Pattern:
   ```python
   import json
   with open(src) as f, open(dst, 'w') as out:
       for line in f:
           rec = json.loads(line)
           for k in ('canonical_path', 'source_path'):
               if k in rec and rec[k]:
                   rec[k] = rec[k].replace('/Volumes/Cherry6TB/', '/Volumes/SG-1-8TB/')
           out.write(json.dumps(rec) + '\n')
   ```
3. **Verify**: count occurrences of the old path on both files; old should be the full manifest length, new should be zero.
4. **Sanity-check a random sample** of 10 rewritten paths against `os.path.exists` on the new volume — if any miss, the migration was incomplete and you need to find/copy the missing files before declaring rewrite done.
5. **Update the manifest summary** if it embeds path examples.

Pair with: also check the runner config / loop.py / any cron entries that reference the old volume. A grep over the project tree (`grep -rln '/Volumes/Cherry6TB' <project-root>`) before declaring migration complete is cheap insurance.

## Cross-volume rescue check before a reformat

When Rick announces a volume will be reformatted/erased and the project has been actively writing to both the old and new volume in parallel, do a quick freshness compare before signing off on the erase:

```python
import os, datetime, sqlite3
for v in ['/Volumes/Cherry6TB', '/Volumes/SG-1-8TB']:
    db = f"{v}/osti_corpus/_state/catalog.sqlite"
    if not os.path.exists(db) or os.path.getsize(db) == 0:
        print(f"{v}: missing or 0-byte"); continue
    sz = os.path.getsize(db)
    mt = datetime.datetime.fromtimestamp(os.path.getmtime(db)).isoformat()
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    rows = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    last_run = con.execute("SELECT MAX(started_ts) FROM refresh_runs").fetchone()[0]
    print(f"{v}: size={sz:,} mtime={mt} papers={rows:,} last_run={last_run}")
    con.close()
```

Newer mtime + larger row count + later last_run → that's the canonical copy. If the old volume is fresher, that's a rescue blocker — pause the reformat and copy the delta first. If equal or older, reformat is safe. Surface the comparison to Rick before he kicks off the reformat.
