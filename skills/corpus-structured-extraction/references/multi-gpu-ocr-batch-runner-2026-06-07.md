# Multi-GPU OCR batch runner — pitfalls and pattern

Session: 2026-06-07. Project: OSTI fulltext corpus, marker-pdf OCR pipeline for
the ~2.6K image-only PDFs identified by the pypdf classification pass (see
`pdf-corpus-extraction-2026-06-06.md`). Target host: uicgpu with 8×A100-80GB.

This file captures the **non-obvious failure modes** of running N independent
single-GPU worker processes (one per GPU) against a partitioned PDF queue.
Use this when scaling any OCR/inference pipeline from 1 GPU to N.

## The architecture (and why)

For marker-pdf specifically — and similar Surya/transformers-backed OCR
stacks — the cleanest way to use N GPUs is **N independent processes, each
pinned to one GPU via `CUDA_VISIBLE_DEVICES`**. NOT a multi-worker
ProcessPoolExecutor inside one Python process, because:

- A single process with `torch.cuda.set_device()` inside workers hits a race
  where multiple workers init on GPU 0 before the per-worker device assignment
  propagates; manifests as `cudaErrorDevicesUnavailable: CUDA-capable
  device(s) is/are busy or unavailable` at the first `.to(device)` call.
- Model loading is global — loading once in the parent + sharing fork-copy-
  on-write across workers either crashes (CUDA contexts can't be forked) or
  silently loads N copies into VRAM on GPU 0.

The right shape:

```
N queue files, one per GPU (round-robin assignment of PDFs)
N detached worker processes, each launched with:
  CUDA_VISIBLE_DEVICES=<idx> python marker_batch.py q<idx>.txt out_dir
  > logs/run.gpu<idx>.log 2>&1 &
each worker writes <id>.md + <id>.json + marker_run.gpu<idx>.jsonl
merge logs at end with jq/cat
```

## Pitfall 1 — relative paths in the queue file

Symptom: every PDF errors with `FileNotFoundError: [Errno 2] No such file or
directory: 'ocr_inbox/smoke100/1001645.pdf'` in `~0.0s` (because the failure
is at file-open, before any CUDA work). On a 100-PDF smoke this looks like
the whole pipeline is broken — all 4 workers crash out cleanly with rate
~3000/s and `ok=0 err=25`. But the actual error message is buried in the
per-record `tb` field of the JSONL log; the worker stdout just shows `err`
status.

Root cause: queue file was built with

```bash
cd /data/stevens && ls ocr_inbox/smoke100/*.pdf > /tmp/q0.txt
```

The `ls` produced relative paths (`ocr_inbox/smoke100/1001645.pdf`). But the
workers were launched without `cd /data/stevens`, so each worker's CWD was
`~stevens`, where that relative path doesn't exist.

Fix: always build queue files with absolute paths.

```bash
ls -d $PWD/ocr_inbox/smoke100/*.pdf | awk 'NR%4==0' > /tmp/q0.txt
```

The `-d` flag makes `ls` print one entry per line and `$PWD/` prefix forces
absolute. Verify with `head -1 /tmp/q0.txt` before launching — first
character should be `/`.

**Pre-flight check** for any queue-based GPU runner:

```bash
head -3 /tmp/q0.txt | awk '{ if (substr($0,1,1) != "/") print "BAD: relative path", $0; else print "OK:", $0 }'
```

## Pitfall 2 — multi-GPU cold-init race even with staggered launch

Symptom: launch 4 workers with `sleep 15` between each (gpu0 → wait 15s →
gpu1 → wait 15s → gpu2 → wait 15s → gpu3), and **one of them** (often gpu1,
sometimes another) still dies at model load with

```
torch.AcceleratorError: CUDA error: CUDA-capable device(s) is/are busy or unavailable
```

The other 3 load fine and run to completion. The failure happens *inside*
the surya foundation predictor `loader.model(device, dtype, ...).to(device)`
call, not at any user-visible torch op.

Root cause: on a host where GPUs were idle for a while (persistence mode
off on uicgpu), the first CUDA op on each card needs to bring up the
driver context, and the multi-process load triggers transient
`cudaErrorDevicesUnavailable` on a subset of cards. 15s staggering helps
but isn't enough — the failure mode is intermittent, not deterministic.

Fix: **pre-warm all GPUs in a single small Python process before launching
workers.**

```bash
ssh uicgpu 'source ~/env.sh && /data/stevens/envs/marker/bin/python -c "
import torch
for i in range(torch.cuda.device_count()):
    x = torch.zeros(4, device=f\"cuda:{i}\")
    print(f\"gpu{i} init ok\")
"'
```

This brings up the driver context on every card in a single process, where
there is no inter-process race. Total cost ~5-10s. Worker launches that
follow this pre-warm see ~0% cold-init failures in subsequent tests.

If pre-warm is impractical, the fallback is to **wrap model load in a 3×
retry with backoff**:

```python
import time
def load_with_retry():
    for attempt in range(3):
        try:
            return create_model_dict()
        except Exception as e:
            if "busy or unavailable" in str(e) and attempt < 2:
                print(f"cold-init race on attempt {attempt+1}, sleeping 10s", flush=True)
                time.sleep(10)
                continue
            raise
```

This costs at most 30s per crashed worker and trades determinism for
recovery.

**Also**: `nvidia-persistenced --persistence-mode 1` (if you have root) keeps
the driver context warm and eliminates this entirely. Worth requesting on
any shared GPU host where you'll run repeated short-lived inference jobs.

## Pitfall 3 — "4GB GPU mem + 0% util" is a worker stuck in CPU post-processing, NOT a dead worker

Symptom: poll `nvidia-smi`, see a worker holding 4-9GB of GPU memory but
0% utilization for minutes at a time. Easy to assume the worker is hung
and kill it.

Don't. Marker (and similar PDF pipelines) interleave **GPU bursts** (OCR
text recognition: ~1-5 seconds at 80-90% util) with **CPU-heavy
post-processing** (PDF rendering, image manipulation, layout reconstruction,
markdown emit). The CPU phase can run 30-300 seconds depending on PDF
complexity, during which the GPU is idle but the worker holds its model
in VRAM.

Diagnostic distinction:

```bash
ssh uicgpu 'ps -ef | grep marker_batch | grep -v grep | awk "{print \$1,\$2,\$3,\$4,\$7}"'
# CPU% column tells the truth:
#   99% CPU usage → process IS working hard, just on CPU
#   0% CPU and 0% GPU → process IS stuck (probably deadlock or waiting on I/O)
#   100-1600% CPU (multi-core) → CPU-heavy post-process phase, expected
```

Cumulative CPU time vs wall time is the canonical signal:

```bash
ssh uicgpu 'ps -o pid,etime,time,cmd -p <PID>'
# ETIME = wall, TIME = CPU. If TIME/ETIME > 1.5x, process is multi-core
# crunching CPU. If TIME plateaus while ETIME grows, process is stuck.
```

Rule: **never kill a worker by GPU-util alone.** Confirm CPU stall first.

## Pitfall 4 — `wait` on a multi-worker launcher shows stale `output_preview`

Symptom: background process launched the 4 worker shells and then sleeps
60s in the launcher. The launcher's `process(action='wait', timeout=60)`
returns a snapshot of stdout from ~before the launcher's own sleep
finished, even though wall-clock minutes have passed. This makes it look
like the launcher itself is stuck.

Root cause: the launcher process exits after its `sleep 60`; the workers
are detached children running independently. `process(action='wait')` only
sees the launcher's buffered stdout, not the workers'. The workers'
output is going to `/data/stevens/ocr_logs/smoke100*.gpu<n>.log`.

Pattern: after the launcher exits, **always check the per-worker logs
directly via ssh**, not the launcher's `output_preview`:

```bash
ssh <host> 'for g in 0 1 2 3; do
  echo "=== gpu$g (latest progress line) ==="
  grep -E "^  \[" /data/<path>/run.gpu$g.log | tail -1
done; echo "=== per-gpu result counts ==="
wc -l /data/<path>/marker_run.gpu*.jsonl'
```

Also worth dumping `nvidia-smi --query-gpu=index,memory.used,utilization.gpu
--format=csv | head -N+1` — gives an instant heartbeat across all GPUs.

## Pitfall 5 — `marker_run.gpu<n>.jsonl` per-worker logs vs a single shared log

The single-worker run wrote `marker_run.jsonl` (one shared log). Multi-worker
runs MUST use per-worker logs — appending JSONL from N processes to one file
without `flock` produces interleaved partial lines that don't parse.

Convention used: `marker_run.gpu<CUDA_VISIBLE_DEVICES>.jsonl` (one per
GPU index). Merge at end with `cat marker_run.gpu*.jsonl > marker_run.jsonl`.

The runner script reads `os.environ['CUDA_VISIBLE_DEVICES']` to derive
its log filename automatically — see `marker_batch.py` at
`uicgpu:/data/stevens/marker_batch.py` for the implementation.

## Throughput numbers (uicgpu, A100-80GB, marker-pdf 1.10.2)

Single-worker baseline: **mean 75s/PDF** (range 36-460s depending on page
count and OCR difficulty). 1.5s/page average.

4-worker multi-GPU (when all 4 stay up): ~3.5× speedup over single-worker
(not 4× because some workers handle outsized PDFs). Projects ~13hr for
2,638 PDFs at 3 GPUs, ~7hr at 8 GPUs if all stay up.

If one GPU drops out of an N-worker run, the others continue cleanly on
their disjoint queue partitions — total time grows proportionally. Worth
adding a "redistribute the failed worker's queue" recovery step for
long runs, OR accepting the partial throughput hit on shorter ones.

## The right launch sequence (canonical)

```bash
# 1. Pre-warm all GPUs (one-shot, ~10s)
ssh <host> 'source ~/env.sh && python -c "
import torch
for i in range(torch.cuda.device_count()):
    torch.zeros(4, device=f\"cuda:{i}\")
"'

# 2. Stage PDFs to GPU host's local fast disk (NOT NFS / network mount)
ssh <src> 'tar czf - -T queue.txt --transform "s|.*/||"' | \
  ssh <gpu_host> 'cd /data/staging && tar xzf -'

# 3. Build ABSOLUTE-PATH partitioned queues
ssh <gpu_host> 'cd /data/staging && for g in $(seq 0 $((N-1))); do
  ls -d $PWD/*.pdf | awk -v g=$g -v n=$N "NR%n==g" > /tmp/q$g.txt
done'

# 4. Verify queues
ssh <gpu_host> 'wc -l /tmp/q*.txt; head -1 /tmp/q0.txt'

# 5. Launch N workers with 15s stagger (defense in depth even with pre-warm)
ssh <gpu_host> 'source ~/env.sh
for g in $(seq 0 $((N-1))); do
  CUDA_VISIBLE_DEVICES=$g nohup <python_path> marker_batch.py \
    /tmp/q$g.txt /data/out > /data/logs/run.gpu$g.log 2>&1 < /dev/null &
  echo "launched gpu$g pid=$!"
  sleep 15
done'

# 6. Monitor (every few minutes)
ssh <gpu_host> 'nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv | head; \
  wc -l /data/out/marker_run.gpu*.jsonl; \
  for g in $(seq 0 $((N-1))); do grep -E "^  \[" /data/logs/run.gpu$g.log | tail -1; done'
```

## When this pattern does NOT apply

- **Single-GPU host**: just run one worker; skip partitioning entirely.
- **Embarrassingly parallel CPU work** (regex extraction, JSON parsing,
  small-prompt LLM calls): use `ProcessPoolExecutor` inside one Python
  process with `--workers N`. The multi-process-per-GPU pattern is only
  necessary when each worker needs a *dedicated* GPU context.
- **Shared remote inference endpoint** (vLLM server, Argo proxy, CELS judge):
  the parallelism lives in the *client*, not the server. Use threads.
