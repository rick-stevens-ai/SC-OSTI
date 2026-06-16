# Throughput packing on Polaris

Verified 2026-06-13 against live `qstat -Qf` and Marker/Nougat published baselines. Refresh annually or after any ALCF queue reorg.

## Polaris queue topology (the part that drives packing)

| Queue            | Min-Max nodes | Walltime | Concurrent cap (per project) | Cost shape          |
|------------------|---------------|----------|------------------------------|---------------------|
| `debug`          | 1–2           | 1h       | 1 running                    | full                |
| `debug-scaling`  | 1–10          | 1h       | 1 queued                     | full                |
| `small`          | 10–24         | 3h       | 10 queued                    | full                |
| `medium`         | 25–99         | 6h       | 10 queued                    | full                |
| `large`          | 100–496       | 24h      | 10 queued                    | full                |
| `prod`           | 10–496        | 24h      | 100 queued                   | router queue        |
| `backfill-small` | 10–24         | 3h       | 10 queued                    | **half (burn_ratio=1)** |
| `backfill-medium`| 25–99         | 6h       | 10 queued                    | half                |
| `backfill-large` | 100–496       | 24h      | 10 queued                    | half                |
| `preemptable`    | 1–10          | 72h      | 10 running / 20 queued       | half, gets evicted  |

Source: `ssh polaris 'qstat -Qf'` filtered for `resources_max/min`, `walltime`, `max_run/queued`, `enable_backfill`.

**The 10-queued-per-project cap is the binding constraint** for non-`prod` queues. You can fan out at most ~10 concurrent jobs per queue, so the design knob is `nodes_per_job × N_jobs`, not one giant job.

## The shared-work-queue / multi-job fan-out trick

Combine the SQLite-work-queue pattern (SKILL.md "Embarrassingly-parallel work") with multiple PBS submissions pointed at the **same** manifest:

- One `prod.sqlite` manifest, one input dir, one output dir.
- N PBS jobs (`small` or `backfill-small`, 10 nodes each) submitted concurrently.
- Workers across all N jobs race for rows via `BEGIN IMMEDIATE` claims. **No manifest splitting, no rank coordination, no harm if jobs land out of order.**
- Effective parallelism = `N × nodes × gpus_per_node`. For Polaris small: `10 × 10 × 4 = 400 GPUs` at full spin-up.

**Restart and preemption are still free** — same property as the single-job version.

## Throughput baselines (Marker + Nougat on A100 40GB)

**MEASURED on Polaris OSTI smoke 2026-06-14** (216-PDF Marker run, 118-done Nougat run, full mixed-size OSTI corpus including PDFs up to 35 MB):

| Tool   | Mean s/PDF | Per-GPU rate | Per-node (4× A100) | Per node-hour | p99 s/PDF | Notes |
|--------|------------|--------------|---------------------|---------------|-----------|-------|
| Marker | 31.5s      | 1.91 PDF/min | 7.6 PDF/min         | **~460 PDF/hr** | 209s | mean dominated by tail; med 23.7s |
| Nougat | 88.5s      | 0.68 PDF/min | 2.7 PDF/min         | **~165 PDF/hr** | 377s | math-heavy subset; max 416s |

**Earlier published-baseline numbers (~18K Marker / 3.6K Nougat per node-hour) were 40× and 22× too optimistic for the OSTI mix.** Those numbers came from clean text-heavy academic PDFs at modest page counts. Real-world OSTI fulltext has multi-MB scanned reports, 50+ page documents, and rendered-equation PDFs that drag the per-PDF mean up 30-100× over best-case.

**Always re-measure on your actual corpus** before sizing prod. The smoke job's `manifest.sqlite` carries `elapsed_s` per row — query `mean(elapsed_s)` after smoke for the real number.

**Sanity check on a real corpus.** 100K PDFs of OSTI shape:

- Marker, 1 node: 100K / 460 ≈ 218 node-hours total compute.
- Marker, 50 nodes × 1 job at 5h walltime = 250 node-hours available → ~15% headroom — TIGHT, use wider+shorter.
- Marker, 100 nodes × 1 job at 3h walltime = 300 node-hours → 38% headroom — comfortable.
- Marker, 10 nodes × 10 jobs at 6h walltime (`backfill-medium` × 10) = 600 node-hours available → 175% headroom — preferred shape for first prod run.
- Nougat, 20% subset (20K): 20K / 165 ≈ 121 node-hours compute. 10 nodes × 3 jobs × 6h = 180 node-hours → 50% headroom.

**Wall-clock is dominated by per-PDF compute at this corpus shape**, not queue wait — at OSTI throughput, even a tight 50-node × 5h pack uses every minute. Width matters.

## Pack-shape decision rule

Given:
- `N_items` = manifest rows
- `R_node` = items/node-hour for the workload (see table above)
- `W_target` = wall-clock you want to hit

Then `nodes_required = N_items / (R_node × W_target)`. Distribute across the 10-queued cap:

| If `nodes_required` ≤ 10        | 1 job in `small` (or `debug-scaling` for ≤10 + ≤1h) |
| If 10 < `nodes_required` ≤ 100  | 2–10 jobs in `small` / `backfill-small`, **same manifest** |
| If 100 < `nodes_required` ≤ 250 | 2–10 jobs in `medium` / `backfill-medium`            |
| If `nodes_required` > 250       | `large` / `backfill-large`, fewer concurrent          |

**Prefer `backfill-*` first**: half-burn for the same throughput, only downside is they sit until idle gaps appear. On a hot cluster (Polaris was ~96% utilized 2026-06-13: 529 busy / 23 free) backfill gaps still exist within an hour or two.

**Escalate `backfill-*` → `small/medium`** only after observing that backfill isn't draining. Don't pay full-burn pre-emptively.

## Two-tier OCR pattern (Marker + Nougat)

Don't run Nougat on the whole corpus — it's 5× slower and only justified on math-heavy PDFs. Use a cheap pre-pass to classify:

1. **Math-density scan** (no GPU, runs on login node or 1 debug node):
   - pymupdf extracts text from first ~20 pages
   - Score on math-char ratio (Greek + `∑∫≈≥≤±→∞`...), LaTeX-pattern frequency, math keyword count
   - Thresholds: `math_ratio > 0.005` OR `latex_per_page > 5` OR `eq_keywords > 20` → `needs_nougat = 1`
   - Column added to the same `prod.sqlite` manifest

2. **Marker pass** (everything, ~18K/hr/node)

3. **Nougat pass** (only `needs_nougat=1` rows, ~3.6K/hr/node)

This typically flags 15–25% of a STEM corpus for Nougat, cutting Nougat compute 4–6× vs running it on everything.

## Pitfalls

- **The 10-queued cap is per-project, per-queue.** Submitting 10 `small` + 10 `backfill-small` works (different queues); submitting 11 `small` doesn't (rejected at qsub time).
- **Don't oversubmit `medium` early.** Wait time is hours when crowded; pick `backfill-small` × N jobs first and only graduate up if it isn't keeping pace.
- **Watch for skewed claim distribution** in the first 5 minutes — if one job's workers monopolize claims (faster-arriving job), the manifest might empty before later jobs start. For very fast workloads, throttle worker start with a small per-rank delay or add a `--max-claims-per-job` budget.
- **`debug-scaling` has `max_queued = 1`** — only one of these in flight per user. Useful for one-shot multi-node smoke; not a packing target.
- **`preemptable` is NOT a backfill substitute.** Eviction loses in-progress work even with the SQLite-claim pattern (claimed row stays `running` until eviction; needs a stale-claim reaper). Backfill jobs run to completion.
