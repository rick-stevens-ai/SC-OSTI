# Marker + Nougat smoke pattern (two-DB, Polaris debug queue)

Captured 2026-06-14 during OSTI corpus OCR pack dispatch on Polaris. This is the cleanest "validate both OCR backends end-to-end before any pilot/prod" pattern, and it sidesteps the schema-mismatch trap between the two workers (see SKILL.md pitfalls).

## When to use this pattern

You're about to dispatch a multi-tier OCR pack (Marker pass over everything + Nougat pass over math-heavy subset) onto Polaris. Before any pilot or prod job, you want independent confirmation that:

1. **Both workers' SQLite-claim loops are functional** with the current env.
2. **Model caches resolve correctly** from `/eagle` (Marker's surya weights, Nougat's checkpoint).
3. **PBS launch contract** (mpiexec → per-rank `CUDA_VISIBLE_DEVICES` → worker python) works for both.
4. **Output paths** (`.md` from Marker, `.mmd` from Nougat) are written atomically and the manifest updates.
5. **Per-PDF runtime profile** is established (warm vs cold) so you can size pilot/prod walltimes honestly.

Two independent smoke DBs are the right shape because the prod two-tier pipeline shares ONE DB (with both schemas merged), but smoke is about validating each worker in isolation — and the smoke runs should not have to migrate the schema yet.

## Step 1: stage 100-250 representative PDFs on `/eagle`

Sample evenly across the input distribution (e.g. 8 PDFs × 27 years = 216 for the OSTI corpus). Flat layout `<eagle>/pdfs/<id>.pdf` matches the worker conventions; don't year-nest in smoke (defer to prod).

```bash
# m1 → cherryrd → polaris two-hop rsync
/opt/homebrew/bin/rsync -av /tmp/smoke_stage/ cherryrd:/tmp/smoke_stage/
ssh cherryrd 'rsync -av /tmp/smoke_stage/ polaris:/eagle/projects/AuroraGPT/stevens/osti_marker/pdfs/'
```

Bandwidth: ~25 MB/s m1→cherryrd, ~99 MB/s cherryrd→polaris (verified 2026-06-14). 600 MB end-to-end in <4 min.

## Step 2: build the two smoke manifests with `build_smoke_dbs.py`

```python
#!/usr/bin/env python3
"""Build paired smoke manifests (Marker manifest + Nougat jobs) from a PDF dir."""
import argparse
import sqlite3
from pathlib import Path


def build_marker(pdf_dir: Path, db_path: Path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS manifest")
    cur.execute("""
        CREATE TABLE manifest (
            id          TEXT PRIMARY KEY,
            pdf         TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            worker      TEXT,
            started_at  REAL,
            finished_at REAL,
            elapsed_s   REAL,
            out_md      TEXT,
            error       TEXT
        )
    """)
    cur.execute("CREATE INDEX idx_status ON manifest(status)")
    for p in sorted(pdf_dir.rglob("*.pdf")):
        rel = str(p.relative_to(pdf_dir))
        cur.execute("INSERT INTO manifest(id, pdf) VALUES (?, ?)", (p.stem, rel))
    conn.commit()
    n = cur.execute("SELECT COUNT(*) FROM manifest").fetchone()[0]
    print(f"marker manifest: {db_path} added={n}")
    conn.close()


def build_nougat(pdf_dir: Path, db_path: Path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS jobs")
    cur.execute("""
        CREATE TABLE jobs (
            id              TEXT PRIMARY KEY,
            pdf_path        TEXT NOT NULL,
            needs_nougat    INTEGER NOT NULL DEFAULT 1,
            nougat_status   TEXT,
            nougat_worker   TEXT,
            nougat_claimed_at REAL,
            nougat_wall_s   REAL,
            nougat_error    TEXT,
            mmd_path        TEXT
        )
    """)
    cur.execute("CREATE INDEX idx_n_status ON jobs(nougat_status)")
    for p in sorted(pdf_dir.rglob("*.pdf")):
        # nougat_worker passes pdf_path straight to nougat CLI — must be absolute
        cur.execute(
            "INSERT INTO jobs(id, pdf_path, needs_nougat) VALUES (?, ?, 1)",
            (p.stem, str(p.absolute())),
        )
    conn.commit()
    n = cur.execute("SELECT COUNT(*) FROM jobs WHERE needs_nougat=1").fetchone()[0]
    print(f"nougat manifest: {db_path} added={n}")
    conn.close()
```

Run on Polaris login with thread caps (always):

```bash
ssh cherryrd 'ssh polaris "cd /eagle/projects/AuroraGPT/stevens/osti_marker && \
  OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
  envs/ocr-py312/bin/python scripts/build_smoke_dbs.py \
    --pdf-dir pdfs \
    --marker-db manifest/smoke_marker.sqlite \
    --nougat-db manifest/smoke_nougat.sqlite"'
```

Verify with python (sqlite3 CLI is not on $PATH — see SKILL.md pitfall):

```bash
ssh cherryrd 'ssh polaris "envs/ocr-py312/bin/python -c \"
import sqlite3
for db, q in [(\\\"manifest/smoke_marker.sqlite\\\", \\\"SELECT COUNT(*) FROM manifest\\\"),
              (\\\"manifest/smoke_nougat.sqlite\\\", \\\"SELECT COUNT(*) FROM jobs WHERE needs_nougat=1\\\")]:
    con = sqlite3.connect(db)
    print(db, con.execute(q).fetchone()[0])
\""'
```

## Step 3: Marker smoke PBS (1 node × 4 GPU, debug queue, 30min)

Key elements that MUST be in the script:

```bash
#PBS -N osti-marker-smoke
#PBS -l select=1:system=polaris
#PBS -l place=scatter
#PBS -l walltime=00:30:00
#PBS -l filesystems=eagle:home
#PBS -q debug
#PBS -A AuroraGPT
#PBS -j oe
#PBS -o /eagle/projects/AuroraGPT/stevens/osti_marker/logs/

set -euo pipefail

BASE=/eagle/projects/AuroraGPT/stevens/osti_marker
cd "$BASE"

module use /soft/modulefiles
module load conda
conda activate "$BASE/envs/ocr-py312"

# Thread caps - prevents OpenBLAS 64-thread spawn on imports
export OPENBLAS_NUM_THREADS=2
export OMP_NUM_THREADS=2
export MKL_NUM_THREADS=2

# Marker model caches - HF + surya separately
export HF_HOME="$BASE/cache/huggingface"
export TORCH_HOME="$BASE/cache/torch"
export TRANSFORMERS_CACHE="$BASE/cache/huggingface"
export MODEL_CACHE_DIR="$BASE/cache/datalab/models"   # <-- surya, NOT covered by HF_HOME
export TOKENIZERS_PARALLELISM=false

export MANIFEST_DB="$BASE/manifest/smoke_marker.sqlite"
export PDF_ROOT="$BASE/pdfs"
export MD_ROOT="$BASE/md/smoke"
mkdir -p "$MD_ROOT"

NODES=$(wc -l < "$PBS_NODEFILE")
GPUS_PER_NODE=4
TOTAL_WORKERS=$((NODES * GPUS_PER_NODE))

# Per-rank launcher (heredoc — written each job invocation)
cat > "$BASE/scripts/marker_worker_launch.sh" << 'EOF'
#!/bin/bash
RANK=${PMI_RANK:-${PALS_RANKID:-0}}
LOCAL_RANK=${PMI_LOCAL_RANK:-${PALS_LOCAL_RANKID:-0}}
export CUDA_VISIBLE_DEVICES=$LOCAL_RANK
export WORKER_ID="$(hostname)_rank${RANK}_gpu${LOCAL_RANK}"
exec python /eagle/projects/AuroraGPT/stevens/osti_marker/scripts/marker_worker.py
EOF
chmod +x "$BASE/scripts/marker_worker_launch.sh"

mpiexec -n "$TOTAL_WORKERS" --ppn "$GPUS_PER_NODE" \
  --depth=16 --cpu-bind depth \
  "$BASE/scripts/marker_worker_launch.sh"

# Final state via python (NOT sqlite3 CLI)
python -c "
import sqlite3
con = sqlite3.connect('$MANIFEST_DB')
for r in con.execute('SELECT status, COUNT(*) FROM manifest GROUP BY status'): print(r)
"
```

## Step 4: Nougat smoke PBS (1 node × 4 GPU, debug queue, 30min)

```bash
#PBS -N osti-nougat-smoke
#PBS -l select=1:system=polaris
#PBS -l place=scatter
#PBS -l walltime=00:30:00
#PBS -l filesystems=eagle:home
#PBS -q debug
#PBS -A AuroraGPT
#PBS -j oe
#PBS -o /eagle/projects/AuroraGPT/stevens/osti_marker/logs/

set -euo pipefail

BASE=/eagle/projects/AuroraGPT/stevens/osti_marker
cd "$BASE"

module use /soft/modulefiles
module load conda
conda activate "$BASE/envs/ocr-py312"

export OPENBLAS_NUM_THREADS=2
export OMP_NUM_THREADS=2
export MKL_NUM_THREADS=2

# Nougat uses HF + torch hub (downloads nougat-base checkpoint to TORCH_HOME)
export HF_HOME="$BASE/cache/huggingface"
export TORCH_HOME="$BASE/cache/torch"
export TRANSFORMERS_CACHE="$BASE/cache/huggingface"
export TOKENIZERS_PARALLELISM=false
# Note: no MODEL_CACHE_DIR needed — Nougat doesn't use surya

DB="$BASE/manifest/smoke_nougat.sqlite"
MMD_DIR="$BASE/mmd/smoke"
mkdir -p "$MMD_DIR"

NRANKS_PER_NODE=4
NODES=$(wc -l < "$PBS_NODEFILE")
TOTAL_RANKS=$((NODES * NRANKS_PER_NODE))

cat > "$BASE/scripts/nougat_worker_launch.sh" << 'EOF'
#!/bin/bash
RANK=${PMI_RANK:-${PALS_RANKID:-0}}
LOCAL_RANK=${PMI_LOCAL_RANK:-${PALS_LOCAL_RANKID:-0}}
export CUDA_VISIBLE_DEVICES=$LOCAL_RANK
DB_ARG="$1"
OUT_ARG="$2"
exec python /eagle/projects/AuroraGPT/stevens/osti_marker/scripts/nougat_worker.py \
    --db "$DB_ARG" --out-dir "$OUT_ARG" --rank "$RANK"
EOF
chmod +x "$BASE/scripts/nougat_worker_launch.sh"

mpiexec -n "$TOTAL_RANKS" --ppn "$NRANKS_PER_NODE" \
    --depth=16 --cpu-bind depth \
    "$BASE/scripts/nougat_worker_launch.sh" "$DB" "$MMD_DIR"

python -c "
import sqlite3
con = sqlite3.connect('$DB')
for r in con.execute('SELECT nougat_status, COUNT(*) FROM jobs WHERE needs_nougat=1 GROUP BY nougat_status'): print(r)
"
```

## Step 5: submit both, watch separately

Marker first (faster, cheaper diagnostic):

```bash
ssh cherryrd 'ssh polaris "cd /eagle/projects/AuroraGPT/stevens/osti_marker && qsub scripts/marker_smoke.pbs"'
# Wait until marker is at least running, optionally wait for ~5 PDFs done
ssh cherryrd 'ssh polaris "cd /eagle/projects/AuroraGPT/stevens/osti_marker && qsub scripts/nougat_smoke.pbs"'
```

Why staggered: debug queue can be shallow, but more importantly if both jobs hit a model-cache-download race (Nougat checkpoint), the second job's workers find the cache already populated and start clean.

## Expected behavior (Polaris debug queue, 2026-06-14 baselines)

### Marker

- **Cold start per worker**: ~30-42s (conda activate + numpy/torch import + Marker model load).
- **Warm runtime per PDF**: ~2-10s for typical OSTI papers (5-20 pages, mostly text).
- **Throughput per A100**: ~5-10 PDF/min warmed, 4 GPUs/node = 20-40 PDF/min/node.
- **216 PDFs on 1 node × 4 GPUs**: ~7-12 min wall.
- **First completion arrives ~30-45s into wall time**, then they stream in.

### Nougat

- **Cold start per worker**: ~60-90s (Nougat checkpoint download + Donut-style model init).
- **Warm runtime per PDF**: ~30-60s (5× slower than Marker).
- **Throughput per A100**: ~1-2 PDF/min warmed, 4 GPUs/node = 4-8 PDF/min/node.
- **216 PDFs on 1 node × 4 GPUs**: ~30-50 min wall — RISKS BUSTING THE 30-MIN DEBUG WALLTIME.

For Nougat smoke, consider: (a) reducing to 100 PDFs, (b) using `debug-scaling` queue with longer walltime cap, or (c) accepting walltime-out and counting partial completions in the audit.

## Audit after smoke

Both workers update `*_error` columns on failure. Bucket failures:

```python
import sqlite3, collections
con = sqlite3.connect("manifest/smoke_marker.sqlite")
errors = con.execute("SELECT error FROM manifest WHERE status='failed'").fetchall()
buckets = collections.Counter()
for (e,) in errors:
    if not e:
        continue
    head = e.split('\n')[0]
    cls = head.split(':')[0]  # exception class
    buckets[cls] += 1
print(buckets.most_common())
```

Promote to pilot only after the failure distribution is understood (see SKILL.md "Submission ladder" section).

## Pitfalls hit this session

1. **Schema mismatch:** the yesterday-scaffolded `build_manifest.py` built `manifest` schema (Marker shape), but the same scaffolding's `nougat_worker.py` expected `jobs` schema. Two-DB approach above sidesteps cleanly; prod will need a one-DB unified schema (deferred).
2. **`sqlite3` CLI absent on Polaris:** PBS lines that echo `Pending: $(sqlite3 ...)` print `command not found` to the job log. Use python sqlite for every inspection.
3. **Pre-warm Python stuck in CPU init for 10+ min:** Marker's `create_model_dict()` instantiates models on CPU after the downloads complete; this is slow on a loaded login node and unnecessary — the cache files are what compute workers need. Kill the pre-warm once `du -sh ~/.cache/datalab/` stabilizes at ~3.0G.
4. **Debug queue depth misleads:** queue showing 12 `H` + 3 `Q` doesn't mean wait; held jobs are skipped by backfill scheduler. Our smoke went Q → R in <30s.
5. **Datalab cache mirror via `cp -rn`:** when the pre-warm landed weights in `~/.cache/datalab/` instead of `/eagle/.../cache/datalab/` (because `MODEL_CACHE_DIR` wasn't set on pre-warm), the recovery is `cp -rn /home/.cache/datalab/* /eagle/.../cache/datalab/`. On a loaded login node this can take 2+ min for 3.0G. The shell tool may timeout at 120s — the cp continues in background; verify with `du -sh` and `pgrep -af "cp -rn"` polls.
6. **Nougat 0.1.17 version-pin chain (2026-06-14, surfaces during Nougat smoke):** unpinned env produces TWO sequential errors that look unrelated. First, `albumentations>=2.0` raises a pydantic schema ValueError on `compression_type` because nougat passes an int (`95`) where the new schema requires a literal. Pin `albumentations<2.0` → next error is `transformers>=4.40` passing a `cache_position` kwarg to nougat's BARTDecoder that the method signature doesn't accept. Pin `transformers<4.40` → smoke runs clean. Fix both pins in `templates/build_ocr_env.sh`; if you only fix the first, you'll get the second on the next launch and waste another env-rebuild cycle. Full diagnosis + root causes in SKILL.md pitfalls.
7. **MPI rank race on `PRAGMA journal_mode=WAL` (2026-06-14, both workers):** fresh SQLite manifest opened simultaneously by 4 ranks → first rank rewrites the file header to WAL, ranks 2-4 race and get `database is locked` before any data write. `connect(timeout=120)` doesn't cover this because the journal-mode header rewrite is outside the busy-retry window. Fix: (a) pre-WAL the DB in `build_smoke_dbs.py` (add `conn.execute("PRAGMA journal_mode=WAL")` + `conn.execute("PRAGMA busy_timeout=60000")` after table create), (b) in each worker, set busy_timeout FIRST then only set WAL if not already WAL. See SKILL.md pitfalls for the canonical worker snippet. Generalizes to any MPI pipeline against a freshly-built SQLite manifest.
