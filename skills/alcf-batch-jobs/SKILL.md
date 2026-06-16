---
name: alcf-batch-jobs
description: "Submit GPU batch jobs to ALCF systems (Polaris A100, Aurora PVC, Crux H100, Sophia). Covers two-hop SSH access, MFA ControlMaster discipline, allocation/quota discovery, the conda-clone-on-eagle env pattern, the SQLite-work-queue + MPI launcher architecture for embarrassingly-parallel GPU jobs (PDF OCR, inference sweeps, evaluation runs), and the smoke→pilot→prod submission ladder. Load BEFORE writing any PBS script for ALCF, BEFORE designing any mass-conversion / mass-inference pipeline that should run there, and BEFORE asking Rick for an MFA seed."
version: 1.0.0
author: Kukla
license: MIT
metadata:
  hermes:
    tags: [ALCF, Polaris, Aurora, Crux, PBS, GPU, batch, OCR, marker, nougat, conda, eagle]
---

# ALCF Batch Jobs

ALCF (Argonne Leadership Computing Facility) systems — Polaris (NVIDIA A100), Aurora (Intel PVC), Crux (NVIDIA H100), Sophia — are Rick's heavy-GPU compute. This skill covers how to land mass-parallel work on them cleanly.

**Don't run large numbers of papers / inferences / evals on uicgpu or chiatta00.** Those are dev nodes. Real bulk goes to ALCF.

## Access

ALCF login nodes are NOT directly reachable from m1. Always two-hop through cherryrd:

```bash
ssh cherryrd 'ssh polaris "<command>"'
ssh cherryrd 'ssh aurora  "<command>"'
ssh cherryrd 'ssh crux    "<command>"'
```

cherryrd's `~/.ssh/config` already aliases `polaris` → `polaris-login-04.alcf.anl.gov` (plus polaris-01..04), `aurora` → `aurora.alcf.anl.gov`, `crux` → `crux.alcf.anl.gov`. Sophia (`alcf-sophia` alias, Tailscale <tailnet-host>) is sometimes flaky from m1 directly — also prefer two-hop.

### MFA ControlMaster seeding (Rick must do this)

ALCF requires CRYPTOCard OTP (MFA). Scripted access from m1 only works AFTER Rick has done one interactive `ssh <system>` from cherryrd, which parks a ControlMaster socket at `~/.ssh/.control_channels/<host>:<port>:<user>`. Sockets are good for ~24h.

Check socket state on cherryrd:
```bash
ssh cherryrd 'ls -la ~/.ssh/.control_channels/'
```

If empty/stale: **stop and ask Rick to do one interactive `ssh polaris` (or aurora/crux) from cherryrd before you go further.** Don't try to script through the MFA prompt — there's no surface for the OTP.

Once seeded, all subsequent `ssh cherryrd 'ssh polaris ...'` calls multiplex through the existing socket. Keep individual commands < 90s wall (the m1→cherryrd ssh has timeout sensitivity); use `nohup ... & disown` on the remote side for long-running work.

### Stale ControlMaster pointing at the wrong polaris-login node

**Failure mode (verified 2026-06-14):** `ssh polaris hostname` from cherryrd hangs ~5-20s then exits 255 with zero output, even when Rick just re-authed. `ssh -v` shows `auto-mux: Trying existing master at '.../polaris-login-04.alcf.anl.gov:22:stevens'` followed by `mux_client_request_session: master session id: N` — the mux client successfully attaches to a stale master at polaris-login-04, but Rick's interactive session is at polaris-login-02. Old master is dead upstream; new auth went somewhere else.

**Diagnosis trick:** Ask Rick where his interactive shell is (`(stevens@polaris-login-02)` in the prompt tells you). If it's NOT `-04`, the generic `polaris` alias (which defaults to `-04` in cherryrd's `~/.ssh/config`) won't ride his fresh auth.

**Fix — use the numbered alias matching Rick's actual login:**
```bash
ssh cherryrd "ssh polaris-02 'hostname; date'"   # rides the live session
```

cherryrd's `~/.ssh/config` aliases `polaris-01`, `polaris-02`, `polaris-03`, `polaris-04` to the respective login nodes. Use whichever matches Rick's live interactive prompt. Don't try `ssh -O exit polaris` to clear the bad master then re-auth — it just sends you back through the same generic alias that may park on the dead node again.

**Belt-and-suspenders:** when Rick says "auth restored," immediately probe with `hostname; date` and check the response is a polaris node name. Silent/empty = wrong alias, not actually failed auth.

## System selection

| System  | GPU                    | Best for                                       | Avoid for                              |
|---------|------------------------|-----------------------------------------------|----------------------------------------|
| Polaris | 4× A100 40GB / node    | PyTorch + CUDA work (Marker, Nougat, vLLM, HF)| nothing structural                     |
| Aurora  | 6× PVC (12 tiles)/node | oneAPI/IPEX-native work, SYCL, MKL examples  | PyTorch-CUDA-only stacks (need IPEX port) |
| Crux    | NVIDIA H100            | Same as Polaris but smaller/newer             | (less documented here)                 |
| Sophia  | A100                   | Inference workloads, smaller jobs             | very-large multi-node                  |

**Default: Polaris.** It's the easiest landing for the PyTorch/CUDA ecosystem (Marker, Nougat, HF transformers, vLLM, etc.). Only pick Aurora when the code is already IPEX-aware (see skill `intel-oneapi-gpu-toolchain`) or when you specifically need PVC.

## Allocation + quota discovery

ALCF projects gate compute hours AND filesystem space. Always check quota before staging large data:

```bash
ssh cherryrd 'ssh polaris "myprojectquotas"'
```

`sbank balance statement` does NOT work (`unexpected arguments: balance, statement`) — `myprojectquotas` is the right command.

Look for:
- **Project name** (3- to 15-char string, used as `-A <project>` in PBS)
- **Used vs Quota** — avoid projects pegged near quota (will fail mid-run)
- **Grace expiry** — avoid expired projects

Listing project dirs you have access to:
```bash
ssh cherryrd 'ssh polaris "ls /eagle/projects/ | head -30"'
```

Rick has access to many projects. Default to `AuroraGPT` on Polaris (vast headroom: 159T / 1.95P as of 2026-06-13). `argonne_tpc`, `tpc`, `LUCID` are also viable. Avoid `datascience` (always tight), expired projects.

## Filesystem layout

Polaris has:
- `/lus/eagle` (91PB shared) — project-grouped, default staging. Use `/eagle/projects/<project>/<user>/`.
- `/lus/grand` — same backend, alternate mount point. Same usage.
- `/home` — 243T, per-user, small. Don't stage bulk data here.

Aurora has `/lus/flare/projects/<project>/<user>/` (91PB, similar pattern; we have `/lus/flare/projects/AuroraGPT/stevens/`).

**Stage all bulk data on the project filesystem, not /home.** Compute nodes mount /eagle and /flare; /home is fine for scripts and configs.

## Conda env pattern (THE recipe)

ALCF systems have system conda modules but NO apptainer (verified on Polaris 2026-06-13 — `module avail apptainer` returns nothing, `/soft/containers/` doesn't exist).

The right pattern for a project-local Python env:

```bash
module use /soft/modulefiles
module load conda                    # default: conda/2025-09-25 → Python 3.12.11 + torch 2.8.0+cuda
conda activate base
conda create --clone base -p /eagle/projects/<project>/<user>/envs/<env-name> -y
conda activate /eagle/projects/<project>/<user>/envs/<env-name>
pip install <your-packages>
```

**Why clone the base env rather than `conda create python=...`?** The base env already has torch built against the cluster's CUDA + correct MPI bindings + NCCL. Cloning preserves that wiring; building from scratch usually breaks the MPI/CUDA linkage.

### Pre-warm model caches on /eagle

Any framework that lazy-downloads weights (HuggingFace, Marker, Nougat, vLLM, etc.) MUST cache to /eagle, not /home (quota) and not per-node /tmp (lost between jobs):

```bash
export HF_HOME=/eagle/projects/<project>/<user>/cache/huggingface
export TORCH_HOME=/eagle/projects/<project>/<user>/cache/torch
export TRANSFORMERS_CACHE=/eagle/projects/<project>/<user>/cache/huggingface
```

Pre-warm by running a model-load script once on a login node BEFORE the first job submission. Otherwise every worker on every node will try to download simultaneously, blow the HF rate limit, and fail.

## Queue + PBS basics

Polaris queues (verified 2026-06-13):

| Queue           | Use for                                      | Walltime cap |
|-----------------|----------------------------------------------|--------------|
| `debug`         | Smoke tests, <100 PDFs / <30min jobs         | 1h           |
| `debug-scaling` | Multi-node smoke                             | 1h           |
| `preemptable`   | Cheap, can be killed; good for resumable work| 72h          |
| `prod`          | Routing queue → small/medium/large           | 24h          |
| `small`/`medium`/`large` | Sized prod queues                   | 24h          |

`small` = 10 nodes, `medium` = 50 nodes, `large` = 100 nodes (typical; check current limits at submit time).

Minimal PBS header:

```bash
#PBS -N <jobname>
#PBS -l select=<N>:system=polaris
#PBS -l place=scatter
#PBS -l walltime=<HH:MM:SS>
#PBS -l filesystems=eagle:home
#PBS -q <queue>
#PBS -A <project>
#PBS -j oe
#PBS -o <log_dir>
```

`-l filesystems=eagle:home` is REQUIRED on Polaris — if the job tries to touch `/eagle` without it declared, it'll fail at job-start. Add `grand` or `flare` if you touch those too.

Submit: `qsub <script>`. Status: `qstat -u stevens`. Cancel: `qdel <jobid>`.

## Pre-flight: inventory the input corpus BEFORE designing the pack

**Hard rule, learned 2026-06-13:** before you propose worker counts, walltimes, queue mix, or any throughput math, you MUST inventory the actual input corpus. If you're about to type a sentence like "we have N PDFs to OCR" — stop and verify N. The cost is one tool call; the cost of skipping is shipping a wrong-shaped pack and re-doing the design after Rick catches it.

The minimum pre-flight, in order:

1. **Enumerate ALL sibling input directories on the target volume.** Not just the one you happened to look at last session. `ls /Volumes/<vol>/ | grep -iE "(<corpus>|pdfs|papers|fetch|recover)"` — long-running projects accumulate sibling staging dirs (`osti_fulltext`, `osti_fulltext_unpay`, `osti_fulltext_v2`, `osti_recovery_<date>`) and pipelining one while ignoring the others ships wrong totals.
2. **Count files, not directory entries.** `find <dir> -name '*.pdf' -type f | wc -l`. `ls | wc -l` includes subdirs, tarballs, scratch state files; the count you want is the actual processable artifacts. Pipe `find` output to a tmp file (`/tmp/all_<corpus>_pdfs.txt`) so subsequent dedup/inventory steps don't re-walk the FS.
3. **Verify structural layout per year/shard.** Real failure 2026-06-13: `osti_fulltext/2016/` had both a flat layout (`2016/*.pdf`, 471 files) AND a nested layout (`2016/2016/*.pdf`, 5450 files) — same OSTI IDs in both, 470 byte-identical duplicates. A naive `find` would have manifested both. Run `find <root>/<shard> -maxdepth N -name '*.pdf' | head` per shard to spot mixed layouts.
4. **Dedup pass before manifest build.** For each basename appearing in 2+ paths, verify byte-identity via sha256 sample (20 pairs is enough). Mixed-layout corpora typically produce flat-vs-nested duplicates that are byte-identical; cross-shard duplicates (same paper in two years) usually aren't. Track which.
5. **Cross-corpus dedup if multiple sibling dirs exist.** Hash basenames (or stable IDs like OSTI ID = leading digits of filename) across `osti_fulltext + osti_fulltext_unpay + osti_fulltext_v2` to find papers fetched twice from different sources. The Polaris manifest should ingest from ALL sources with cross-corpus dedup keyed on stable ID, not just one.
6. **Check for in-flight rsync / fetch processes.** `ps -ef | grep -E "(rsync|fetch|<corpus>)" | grep -v grep`. If the corpus is still landing, decide explicitly: wait for it to drain, OR build the manifest now and re-dedup after the sweep. Don't pretend the corpus is static when it isn't.
7. **Report the FULL year-range coverage, not just the years you happened to find first.** If the user's mental model is "all years 2006-onward" and you've only enumerated 2016+, you're going to ship a pipeline that silently drops the pre-2016 work. Loop `for y in 2006..2026; do ... done` and report the per-year counts; explicit zeros surface gaps that aggregated totals hide.

`scripts/inventory_corpus.sh` packages steps 1-7 for the OSTI Cherry6TB shape; copy and adapt for other corpora. Output is a 3-section report (sibling dirs × structural layout × per-year counts) suitable for pasting into a pack-design doc.

**Failure modes this catches:**
- Designing for 160K PDFs when the actual count is 67,590 (off by 2.4× → walltime estimate wrong by same factor → wrong queue choice).
- Missing entire year ranges (pre-2016 in `osti_fulltext_unpay` ignored because I only walked `osti_fulltext`).
- Manifesting 470 byte-identical duplicates because flat-vs-nested 2016 layout was unnoticed.
- Manifesting from one source dir while three others on the same volume hold thousands more papers from the same project.

The pre-flight takes 2-3 minutes. The downstream design depends on its numbers being right.

## Embarrassingly-parallel work: SQLite work-queue + MPI launcher

For "N independent items, one GPU each" workloads (PDF OCR, per-item inference, per-paper extraction, sweep cells), DO NOT submit N PBS jobs. Submit ONE multi-node job with a shared work queue.

Architecture:

```
manifest.sqlite  (rows: id, input_path, output_path, status pending|running|done|failed)
       ↑
mpiexec -n <total_workers> --ppn <gpus_per_node>  worker.sh manifest.sqlite
       ↓
each worker (one per GPU):
  loop:
    BEGIN IMMEDIATE; SELECT next pending row; UPDATE status=running; COMMIT
    process item (Marker, Nougat, model inference, whatever)
    write output atomically (tmp + rename)
    UPDATE status=done|failed
```

**SQLite + `BEGIN IMMEDIATE` handles concurrent claims correctly** across hundreds of workers on a shared filesystem. flock-on-jsonl works for small jobs but races at scale. Stdlib `sqlite3` — no extra dependencies.

**Restart = free.** Re-launching the same job skips `done` rows. PBS preemption / walltime-out → resubmit and it picks up where it left off. This is the killer feature; design around it from the start.

**Sizing.** Polaris: 4× A100/node. Throughput depends on workload but for a planning baseline assume each A100 does ~5-10 items/min for medium ML workloads. Estimate total walltime as `items / (nodes × 4 × items_per_min_per_gpu)`. Oversize walltime; cost of an early-finish is zero, cost of a walltime-out is a resubmit.

**Marker / Nougat specifics**: Measured on Polaris A100 from the 2026-06-14 OSTI smoke (216 PDFs, mixed sizes/eras): Marker mean 31.5s/PDF = ~1.9 PDFs/min/GPU = **~458 PDFs/hr/node** (4 GPUs). Nougat mean 88.5s/PDF = ~0.68 PDFs/min/GPU = **~163 PDFs/hr/node**, ~3× slower than Marker; only worth running on a math-heavy subset (see "Two-tier OCR" below). These measured numbers are ~40× lower than the original synthetic planning baselines (which assumed light single-page PDFs) — use the measured ones for any real corpus sizing. For a 100K-PDF Marker pass: ~220 node-hours = **50 nodes × ~4.5h walltime** with 25% headroom = fits `medium`. The smoke also showed 100% Marker success rate (216/216 done) and a clean two-DB Marker+Nougat split (Marker manifest had 216/216 done; Nougat `jobs` table had 118 done + 4 stale `claimed` + 94 NULL where `needs_nougat=0`). The 4 `claimed` rows are reapable stalls — design the prod loop to reap rows in `claimed` state older than ~15 min back to `pending`.

**Multi-job fan-out against ONE manifest.** The 10-queued-per-project cap on most queues means a single huge job is rarely optimal. Submit N PBS jobs (`small` or `backfill-small`, 10 nodes each) all pointing at the SAME `prod.sqlite`. Workers race for rows across jobs via `BEGIN IMMEDIATE`; no manifest splitting, no rank coordination, restart still free. Effective parallelism = `N × nodes × 4` GPUs (Polaris small: up to 400 GPUs at full spin-up).

**Prefer `backfill-*` first**: same throughput, half-burn (burn_ratio=1). Even on a hot cluster, backfill gaps appear within an hour or two. Escalate to full-burn `small`/`medium` only after observing backfill isn't keeping pace.

See `references/throughput-packing.md` for live queue table, throughput baselines, pack-shape decision rule, and the two-tier OCR pattern. See `scripts/marker_worker_template.py` and `templates/pbs_marker.sh` for working examples (PDF OCR via Marker on Polaris).

## Two-tier OCR (Marker + Nougat)

Don't run Nougat on the whole corpus — it's ~5× slower than Marker. Use a cheap pre-pass to flag the math-heavy subset:

1. **Math-density scan** (no GPU, ~30 min for 160K PDFs on one login or debug node): pymupdf extracts text from first ~20 pages; score on math-char ratio (Greek + `∑∫≈≥≤±→∞`...), LaTeX-pattern frequency, math keyword count; thresholds `math_ratio > 0.005` OR `latex_per_page > 5` OR `eq_keywords > 20` → mark `needs_nougat=1` in the same manifest.
2. **Marker pass** on everything (one fan-out of 10-node `backfill-small` jobs).
3. **Nougat pass** on `needs_nougat=1` only (same fan-out shape, smaller manifest subset, separate output dir).

This typically flags 15–25% of a STEM corpus, cutting Nougat compute 4–6× vs running it on everything. Both passes share the same SQLite, same atomic-claim contract, same restart semantics.

## Submission ladder (smoke → pilot → prod)

Never go straight to full scale. Every new pipeline runs this ladder:

| Stage  | Size       | Queue         | Nodes × Walltime | Purpose                                 |
|--------|------------|---------------|------------------|------------------------------------------|
| Smoke  | 50-200 items | `debug`     | 1 × 30min        | Validate env, worker loop, output schema |
| Pilot  | 2-10K items | `preemptable` or `small` | 2-4 × 4h | Validate throughput, surface failure modes |
| Prod   | Full       | `prod` / `medium` | sized to ETA | Run it                                  |

After each stage: **audit failure buckets** (parse error / OOM / corrupt input / timeout / model-loading flake / etc.) before promoting. Don't promote on success-rate alone — promote on understanding the failure distribution.

## Output retrieval

Pull results back via cherryrd:

```bash
ssh cherryrd 'rsync -av polaris:/eagle/projects/<project>/<user>/<run>/output/ /tmp/<run>_output/'
# then m1 pulls from cherryrd, or scp directly through the two-hop
```

**Don't delete the /eagle staging until the local mirror is verified** (count + size + spot-check a few files).

## Pitfalls

- **Don't try to script through MFA.** If the ControlMaster socket on cherryrd is empty, ask Rick to do one interactive `ssh polaris` first. Don't burn 10 minutes hitting the OTP wall.
- **The m1→cherryrd ssh has a soft ~60s timeout in Hermes.** For any Polaris command that may run longer (env build, model pre-warm, large rsync), use `nohup <cmd> > log 2>&1 & disown` on the remote side and poll the log separately.
- **`sbank balance statement` doesn't work** — use `myprojectquotas` to see quota; for compute-hour balances, ALCF docs say `sbank-list-allocations` (untested by me).
- **`-l filesystems=...` is REQUIRED** on Polaris PBS. Forgetting it → job dies at start with no clear error.
- **No apptainer on Polaris.** Don't waste time looking for a container path; conda-clone is the pattern.
- **Don't build a Python env from scratch.** Clone base. Otherwise you'll fight MPI/CUDA linkage all day.
- **Pre-warm model caches BEFORE first job submission.** Otherwise hundreds of workers race to HuggingFace and you hit rate limits.
- **Polaris login nodes cap user threads — numpy/BLAS imports segfault by default.** First numpy/torch/marker import on a login node fails with `OpenBLAS blas_thread_init: Resource temporarily unavailable` (login tries to spin 64 BLAS threads against a per-user thread cap). ALWAYS export `OPENBLAS_NUM_THREADS=2`, `OMP_NUM_THREADS=2`, `MKL_NUM_THREADS=2` BEFORE any python invocation on login nodes (pre-warm, smoke imports, manifest builders, anything). On compute nodes the limits are higher and 4-8 is fine; the login-node failure is the one that bites. The pre-warm template (`templates/build_ocr_env.sh`) bakes these in — keep them when adapting.
- **Marker uses surya OCR backend which caches OUTSIDE `HF_HOME`.** surya downloads to `~/.cache/datalab/models/` by default (4 model packs: `layout/`, `text_detection/`, `text_recognition/`, `table_recognition/`, ~3.0G total). Setting `HF_HOME`/`TORCH_HOME`/`TRANSFORMERS_CACHE` does NOT redirect this — surya's `Settings` class uses its own `MODEL_CACHE_DIR` attribute (no `env_prefix`, the env var is literally `MODEL_CACHE_DIR`). To land surya weights on /eagle, ALSO export `MODEL_CACHE_DIR=$BASE/cache/datalab/models` everywhere (pre-warm AND worker PBS scripts). If you forget on pre-warm: cache lands in `~/.cache/datalab/`; mirror it post-hoc via `cp -rn ~/.cache/datalab/* $BASE/cache/datalab/` (slow on a busy login — 3G can take 2+ min over the home→eagle path) and set `MODEL_CACHE_DIR` in worker env. On Polaris /home IS shared across compute nodes so the un-mirrored case still works, but it's wasteful and breaks on other ALCF systems where /home is per-node.
- **One PBS job per item is wrong.** Use the SQLite work-queue pattern.
- **`/home` is small (243T shared).** Stage bulk on `/eagle`.
- **`module load conda` alone doesn't activate Python.** Need `conda activate base` after.
- **Marker on Aurora needs IPEX port** — Marker is PyTorch-CUDA-native; the PVC path requires Intel Extension for PyTorch and surya OCR backend port, both untested as of 2026-06-13. For Marker bulk work, Polaris.
- **`preemptable` is NOT a backfill substitute** for stateful work. Eviction loses any in-progress claim (row stays `running` until you reap it); backfill-* jobs run to completion. Use `backfill-*` for SQLite-claim workloads, `preemptable` only when each item is short enough that mid-item eviction is cheap.
- **Throttle-or-time skewed claim distribution.** In multi-job fan-out, the first job's workers can drain the manifest before later jobs start. Either oversize the manifest relative to one-job capacity, OR add a small startup delay per rank, OR run a stale-claim reaper that resets `running` rows older than N minutes back to `pending`.
- **Walltime SIGKILL leaves rows stuck in `claimed`/`running` forever.** Verified 2026-06-14 Nougat smoke v4: PBS walltime 45min, but the tail of large PDFs (35 MB / 13 MB / 3 MB / 1 MB) was still in flight when `=>> PBS: job killed: walltime 2783 exceeded limit 2700` fired and SIGKILL'd all 4 ranks mid-claim. No partial mmd written, no error logged, no nougat_wall_s set — rows just sit at `nougat_status='claimed'` indefinitely. **The fix is the stale-claim reaper at `scripts/reap_stale_claims.py`** — resets rows in `claimed` (Nougat) or `running` (Marker) for > N minutes back to `pending`, clearing worker assignment so the next job re-claims them. Run it (a) after any walltime-killed job, (b) periodically during long fan-out runs as a sweeper cron, (c) as part of the prod loop between back-to-back job submissions. Idempotent; safe to dry-run with `--dry-run` first. **Diagnostic giveaway for walltime kill vs real worker hang**: check `<smoke>.err` for `=>> PBS: job killed: walltime` — if present and the stuck rows' `*_claimed_at` (or `started_at`) timestamps cluster in the final ~minute of the job's walltime window, it's a walltime truncation, not a worker bug. Always reap before re-running; don't re-submit against a manifest with stale claims (the row will be skipped because it's not `pending`).
- **The 10-queued cap is per-project, per-queue.** 10 `small` + 10 `backfill-small` works (different queues); 11 `small` is rejected at qsub.
- **`sqlite3` CLI is NOT on Polaris $PATH** (neither login nor compute). Any PBS line like `Pending: $(sqlite3 "$DB" 'SELECT ...')` will emit `bash: sqlite3: command not found` to the job log. The line is cosmetic if you don't depend on its output — `set -euo pipefail` doesn't fail it because `$(...)` swallows the error — but it pollutes the log. For manifest inspection inside PBS, use python sqlite: `python -c "import sqlite3; c=sqlite3.connect('$DB'); print(c.execute('SELECT COUNT(*) FROM manifest WHERE status=\"pending\"').fetchone()[0])"`. Same on login nodes when poking at manifests.
- **Don't pin shared envs down to satisfy ONE unmaintained tool — build a sibling conda env instead.** Nougat 0.1.17 needs `transformers<4.40` (see next bullet), but the shared `ocr-py312` env on /eagle has many co-tenants — `marker-pdf`, `surya-ocr`, `sglang`, `vllm`, `compressed-tensors`, `peft`, `sentence-transformers`, `verl`, `xgrammar`, `mamba-ssm` — that all depend on modern transformers (4.57+). Pinning shared env transformers down to 4.39 breaks every other tool. **Right pattern: parallel sibling env per tool with conflicting deps.** `envs/ocr-py312/` keeps modern transformers for Marker + everything else; `envs/nougat-py312/` is built fresh with pinned `transformers<4.40` + `albumentations<2.0`. PBS scripts activate whichever env they need. Cost: ~20 GB extra on /eagle per sibling env, ~3-8 min `conda create --prefix envs/<name> python=3.12 -y && pip install <pinned-set>` build. Pays off forever — and matches Rick's preference for clean infrastructure over fragile shared state. Document the per-tool env list in the project README so future-you doesn't accidentally activate the wrong one. Generalizes beyond OCR: any unmaintained-but-still-useful library (vintage nougat, donut, layoutparser, older fairseq, etc.) should get its own sibling env, not contaminate the workhorse one.
- **Pre-stage external model checkpoints to /eagle BEFORE running workers; set per-tool checkpoint env var.** Some OCR/inference tools download large weights from public hosts (GitHub releases, HuggingFace, model-specific CDNs) on first invocation. When N workers fire simultaneously without a pre-staged cache, they ALL race to download the same multi-GB file → rate-limit, partial-file corruption, or 30+ min worker startup wall. **Pre-stage to a known-stable path on /eagle and tell each worker via env var.** Examples:
  - **Nougat** (`nougat-ocr 0.1.17`, ~1.4G `pytorch_model.bin` from github.com/facebookresearch/nougat/releases): pre-download 5 files (`config.json`, `pytorch_model.bin`, `special_tokens_map.json`, `tokenizer.json`, `tokenizer_config.json`) into `$BASE/cache/nougat-0.1.0-base/`, set `export NOUGAT_CHECKPOINT=$BASE/cache/nougat-0.1.0-base` in PBS env block. `nougat-ocr` reads that env var first, skips its `torch.hub` download. Pre-download from a login or m1 (~18s on Polaris login GH connection) before any worker fires. Verify all 5 files present + correct sizes BEFORE submitting.
  - **Marker / surya** — see `MODEL_CACHE_DIR` pitfall above; same shape, different env var.
  - **HuggingFace models** — `HF_HOME` + pre-warm via single login-side `from_pretrained(...)` invocation.
  Generalizes: any worker that has a "first-fetch slow / cached fast" pattern needs the cache pre-staged on /eagle (or whichever fast shared FS the workers see) and a per-tool env var pointed at it. Without this, "smoke worked" can lie about "prod will work" — smoke had 4 workers downloading once; prod has 200 workers all racing.
- **The PBS script and the worker contract MUST match — env-vars vs CLI args is a silent failure mode.** Yesterday's `marker_prod.pbs` (left over from a prior pack design) launched workers with `python marker_worker.py --db ... --pdf-dir ... --out-dir ... --rank ...`. Today's verified `marker_worker.py` reads `MANIFEST_DB`, `PDF_ROOT`, `MD_ROOT`, `WORKER_ID` from env. If you grab a PBS template from a prior session and the worker has evolved (or vice versa), it will start, the CLI flags will be ignored as unknown args (depending on argparse mode), and the worker will crash on `os.environ['MANIFEST_DB']` KeyError — or worse, silently use a stale default and write into the wrong manifest. **Pre-flight before re-submitting any stale PBS:**
  1. `head -50 marker_worker.py` — note all `os.environ[...]` reads at module top.
  2. `head -50 marker_prod.pbs` — confirm the env block exports all of those, OR the per-rank launcher does.
  3. Mismatch = rewrite the PBS to match the worker, NOT the other way around. Worker contract is more stable; PBS is per-pack scaffolding.
  Same rule applies for nougat smoke vs prod, any future tool you add to the pipeline, and any worker you adopt from elsewhere. The contract surface (env vars vs CLI args vs config file) is the first thing to verify when promoting smoke → prod.
- **nougat-ocr 0.1.17 is unmaintained and needs a TWO-pin chain to run on a fresh Polaris env (2026-06-14):** `pip install "albumentations<2.0" "transformers<4.40"`. The failure chain when these aren't pinned, in order of which surfaces first:
  1. **`albumentations>=2.0`** (current default 2.0.8+) → `ValueError: 1 validation error for InitSchema / compression_type / Input should be 'jpeg' or 'webp' [literal_error, input_value=95]` at worker startup. Root cause: albumentations 2.0 migrated to strict pydantic v2 schemas; nougat passes `95` (int) where the new schema requires literal `'jpeg'|'webp'`. Pin to `albumentations<2.0` → 1.4.24 works.
  2. **`transformers>=4.40`** (any modern HF default) → `TypeError: BARTDecoder.prepare_inputs_for_inference() got an unexpected keyword argument 'cache_position'` during first generation call. Root cause: transformers' generation loop now passes `cache_position` kwarg to model's `prepare_inputs_for_inference()`; nougat 0.1.17's BARTDecoder doesn't accept it. Pin to `transformers<4.40` OR monkey-patch the method to swallow the kwarg. Pin is the safer route — nougat's BARTDecoder is a heavily-modified subclass and patching risks breaking generation in other ways.
  Apply BOTH pins at env-build time (in `templates/build_ocr_env.sh`); only diagnosing one and patching the other gives you the next error 30 seconds later. If you discover a third version-skew error after these two, capture it here — the unmaintained-since-2023 reality means more dependencies will drift over time.
- **Walltime kill leaves stale `claimed` / `running` rows that never re-process — need a reaper.** When a PBS job hits its walltime limit, PBS sends SIGKILL to all ranks mid-task. Any row a worker had `claimed`/`running` at kill-time stays that way forever — no error, no completion, just stuck. Verified 2026-06-14 nougat smoke (45min walltime, 4 ranks all on same node x3001c0s13b1n0, claimed their last PDFs within 80s of each other, killed by `walltime 2783 exceeded limit 2700` from stderr — 38h later 4 rows still showed `nougat_status='claimed'` with valid worker IDs and no errors). Diagnosis tell: `nougat_wall_s IS NULL` and `nougat_error IS NULL` for stuck rows; PBS log shows `walltime N exceeded limit M`. **Fix pattern**: a stale-claim reaper that resets rows where `claimed_at < now - threshold_minutes` back to pending. Canonical impl at `/eagle/projects/AuroraGPT/stevens/osti_marker/scripts/reap_stale_claims.py` (handles both marker `manifest(status=running, started_at=ISO)` and nougat `jobs(nougat_status=claimed, nougat_claimed_at=epoch)` schemas; --dry-run + --stale-minutes flags). Run after every job that gets walltime-killed, or schedule before the next launch — idempotent + safe to run multiple times. Records `error='reaped_walltime_kill'` for audit. Without this, every walltime kill burns 1-4 rows permanently and re-runs claim only the remaining `pending` rows, never the orphaned `claimed` ones. Generalizes beyond OCR — any SQLite work-queue + MPI pattern with PBS walltime needs a reaper. Skill template should include reaper in standard pipeline tooling, not as an afterthought.

- **MPI rank race on `PRAGMA journal_mode=WAL` kills 3 of 4 workers at startup on a fresh SQLite manifest.** When N MPI ranks open the same manifest DB simultaneously and each does `PRAGMA journal_mode=WAL`, the first rank rewrites the file header to WAL mode; ranks 2-N race on the same write and the SQLite engine returns `sqlite3.OperationalError: database is locked` even before any data write. `connect(timeout=120)` does NOT cover this — the journal-mode header rewrite is outside the busy-retry path. **Fix in two parts** (do both):
  1. **Pre-WAL the DB during manifest build**, not at first worker open. Add to the manifest-builder script:
     ```python
     conn.execute("PRAGMA journal_mode=WAL")
     conn.execute("PRAGMA busy_timeout=60000")
     ```
     Verify before launch: `python -c "import sqlite3; print(sqlite3.connect('manifest.db').execute('PRAGMA journal_mode').fetchone())"` should print `('wal',)`.
  2. **Patch the worker to skip the WAL pragma if already in WAL**, AND set busy_timeout FIRST:
     ```python
     cur.execute("PRAGMA busy_timeout=60000")
     cur_mode = cur.execute("PRAGMA journal_mode").fetchone()[0]
     if cur_mode != "wal":
         cur.execute("PRAGMA journal_mode=WAL")
     ```
     The busy_timeout-first ordering matters: if the pragma fires, the retry window is in place to handle any residual contention.
  Generalizes to any embarrassingly-parallel MPI pipeline against a freshly-built SQLite manifest. Cost of skipping: 3 of 4 workers die at startup, the smoke looks broken, and you spend 20 min diagnosing what's actually a one-line fix.
- **Marker and Nougat workers use DIFFERENT SQLite schemas.** Marker (`marker_worker.py`) reads/writes `manifest(id, pdf, status, worker, started_at, finished_at, elapsed_s, out_md, error)`. Nougat (`nougat_worker.py`) reads/writes `jobs(id, pdf_path, needs_nougat, nougat_status, nougat_worker, nougat_claimed_at, nougat_wall_s, nougat_error, mmd_path)`. If you ship them against a single shared DB (the prod intent: math-density scan flags `needs_nougat=1` on rows), the DB must satisfy BOTH schemas — workers don't auto-translate. Two design choices:
  - **One-DB unified**: build a `jobs` table with marker columns (`status`, `out_md`, `elapsed_s`, ...) AND nougat columns (`needs_nougat`, `nougat_status`, `mmd_path`, ...) in one row; patch `marker_worker.py` to read/write `jobs` instead of `manifest` (rename the table references). Required for the two-tier scan→Marker→Nougat pipeline.
  - **Two-DB independent** (smoke shape): `smoke_marker.sqlite` with `manifest` table for Marker, `smoke_nougat.sqlite` with `jobs` table for Nougat, both seeded from the same PDF dir. Workers are untouched, smoke runs are independent, no schema migration needed.
  See `references/marker-nougat-smoke-pattern-2026-06-14.md` for the working two-DB smoke recipe (`build_smoke_dbs.py` + paired PBS scripts).
- **Debug queue depth ≠ wait time.** `qstat debug` showing 12 jobs in `H` (held) state and 3 in `Q` doesn't mean you'll wait — Polaris backfill scheduler picks debug jobs from queued state in priority order, ignoring held jobs entirely. Real wait depends on backfill gaps on the compute side. Empirical 2026-06-14: submitted to debug with ~15 queue entries, job went from Q → R in <30s because most were `H` from other users' constraints. Don't pre-cancel a submit because "debug looks full."
- **`qstat -u <user>` returns ONLY active jobs (queued/held/running). Empty output != "nothing happened today."** Verified 2026-06-15: user asked "how are the polaris runs going?" — bare `qstat -u stevens` returned empty (exit 0, blank stdout), which would have led to "no runs" when in fact there was a rich finished-job history (OCR smoke tests, parsl jobs, older PeleC) that surfaced the actual state. Use `qstat -u stevens -x` to include finished jobs (state `F`), or `qstat -u stevens -H` for history-only. The `-x` flag is the right default when diagnosing "what's the recent activity?" rather than "is anything running right now?". Reading the recent `osti-marker-*`, `osti-nougat-*`, `nougat_smoke*` job names in the history tells you exactly what stage the user/peer agent is at without having to mailbox-ping them.
- **`#PBS -q debug` job's `Resource_List.burn_ratio`** appears in `qstat -f` (e.g. 0.7672 seen 2026-06-14) and is for the project, NOT this job. Debug queue itself doesn't burn allocation; it's there to provide a project-level burn signal. Don't read it as "this 30-min smoke job will eat 76% of allocation."
- **Pre-warm Python on login can hang in CPU model-init AFTER downloads complete.** Surya/datalab models download fast (~80 MB/s to /home/.cache) but Marker's `create_model_dict()` then spends 5-20 min instantiating models on CPU (no GPU on login). The DOWNLOADED CACHE is what compute workers need, NOT the live Python object. Once `du -sh ~/.cache/datalab/` stabilizes at the expected size (~3.0G for surya), KILL the pre-warm process — it's done its job. Don't wait for the python `print("Models loaded:", ...)` line; that may never arrive on a loaded login.
- **Don't pipe tar through `pv` unless you've verified `pv` is installed on the source host.** Verified 2026-06-14 on m1: `tar -cf - . | pv -s 320G | ssh cherryrd 'ssh polaris-02 "tar -xf -"'` failed in <2s with `bash: pv: command not found` on the producing side, then `tar: This does not look like a tar archive / tar: Exiting with failure status due to previous errors` on the consuming side. The misleading second error is the symptom — the receiving tar saw 0 bytes (pv exited immediately, pipe closed), and reports it as a malformed archive, NOT as a missing-pv error. Easy to misdiagnose as a transport problem and waste cycles debugging the wrong layer. **Two fixes:** (a) install pv on m1: `brew install pv`. (b) just omit pv: `tar -cf - . | ssh cherryrd 'ssh polaris-02 "cd /target && tar -xf -"'`. You lose the live progress bar but the transfer works. To get progress without pv, periodically poll the destination via `ssh ... 'du -sh /target; ls /target | wc -l'` from the parent session. See `references/tar-pipe-bulk-corpus-transfer.md` for the full two-hop pattern incl. expected throughput.

## Linked

- `references/polaris-quick-facts.md` — current quota landscape, queue limits, module versions, working examples (refresh annually or after any ALCF migration).
- `references/throughput-packing.md` — live queue topology, Marker/Nougat throughput baselines, pack-shape decision rule, two-tier OCR pattern, multi-job fan-out pitfalls.
- `references/corpus-inventory-before-throughput-design-2026-06-13.md` — worked failure where I shipped a 160K-PDF pipeline design without inventorying the corpus; real count was 67,590 in one dir + 24,427 in another + 10,726 still landing, with 470 byte-identical dupes inside one dir and 2006-2015 coverage hidden in a sibling dir I'd never enumerated. Read BEFORE proposing throughput on any large-corpus pipeline.
- `references/marker-nougat-smoke-pattern-2026-06-14.md` — working two-DB Polaris debug-queue smoke recipe for Marker + Nougat (paired manifests, PBS scripts, sqlite-CLI workarounds, expected timings, audit pattern). Use BEFORE any pilot/prod OCR pack dispatch.
- `references/tar-pipe-bulk-corpus-transfer.md` — two-hop tar-pipe recipe for pushing tens of thousands of small files from m1 to /eagle, with throughput baselines, pitfalls (the pv-missing silent failure), and recovery patterns. Use BEFORE designing any bulk-corpus push to ALCF that involves >10K files.
- `templates/pbs_marker.sh` — known-good PBS script for Marker OCR on Polaris.
- `templates/build_ocr_env.sh` — conda clone + marker-pdf/nougat-ocr install + pre-warm.
- `scripts/probe_polaris.sh` — re-runnable Polaris environment probe (modules, quotas, queues, GPU layout).
- `scripts/inventory_corpus.sh` — pre-flight corpus inventory (sibling dirs × per-year coverage × mixed-layout × in-flight processes × cross-dir overlap). Run BEFORE the pack-design conversation.
- `scripts/reap_stale_claims.py` — stale-claim reaper for Marker (`running`) and Nougat (`claimed`) manifests. Run after any walltime-killed job to reset abandoned rows back to `pending` so the next submission re-processes them. Also good as a periodic sweeper cron during long prod fan-out runs. Dry-run with `--dry-run`.

## Related skills

- `intel-oneapi-gpu-toolchain` — when targeting Aurora PVC and validating MKL/SYCL/OMP5 stacks.
- `qe-pvc-build-chiatta00` — for Quantum ESPRESSO on chiatta00 (sister-class oneAPI work, not ALCF).
- `dropbox-file-recovery` — when source data on m1 is online-stub instead of materialized.
- `sanitize-for-external-share` — before any ALCF outputs leave Rick's trust boundary.
