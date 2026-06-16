# Polaris quick facts

Last verified: 2026-06-13 via Kukla probe through `ssh cherryrd 'ssh polaris ...'`.

Refresh annually or after any ALCF migration. Compare to https://docs.alcf.anl.gov/polaris/ for canonical detail.

## Login

- Aliased on cherryrd: `polaris` → `polaris-login-04.alcf.anl.gov`
- Round-robin alternates: `polaris-01..04` for direct-login variants
- User: `stevens`
- Two-hop required from m1: `ssh cherryrd 'ssh polaris "..."'`

## Filesystems (`df -h`)

| Mount         | Size | Used | Avail | Notes                          |
|---------------|------|------|-------|--------------------------------|
| `/lus/eagle`  | 91P  | 58P  | 33P   | Primary project storage        |
| `/lus/grand`  | 91P  | (clone of eagle) | | Alternate mount of same backend |
| `/home`       | 243T | 80T  | 161T  | Per-user, small — scripts only |

## Allocations Rick can use (`myprojectquotas` excerpt)

| Project                | FS      | Used   | Quota  | Notes                              |
|------------------------|---------|--------|--------|------------------------------------|
| **AuroraGPT**          | /eagle  | 159.8T | 1.953P | **Default. Vast headroom.**        |
| argonne_tpc            | /eagle  | 1.249P | 10P    | Also fine                          |
| tpc                    | /eagle  | 185.5T | 10P    | Also fine                          |
| LUCID                  | /eagle  | 14.81T | 50T    | OK for smaller runs                |
| CVD-Mol-AI             | /eagle  | 1.353P | 1.465P | Tight                              |
| Candle_ECP             | /eagle  | 2.095T | 0      | Quota=0 (expired/over)             |
| candle_aesp            | /eagle  | 17.65T | 1M     | Quota=1M (expired)                 |
| FoundEpidem            | /eagle  | 757.6T | 1M     | Quota=1M (expired)                 |
| radbio                 | /eagle  | 1.069T | 1T     | **EXPIRED grace** — avoid          |
| datascience            | /eagle  | 284T   | 320T   | Tight, leave for others            |
| datascience_collab     | /eagle  | 12.04T | 11T    | OVER quota, grace running          |
| ModCon                 | /eagle  | 6.245T | 1000T  | OK                                 |
| IMPROVE_Aim1           | /eagle  | 23.03T | 250T   | OK                                 |
| AGI-for-Science        | /grand  | 6.62G  | 1M     | Quota=1M (expired)                 |
| CSC249ADOA01           | /grand  | 13.86T | 98T    | OK                                 |

Default to **AuroraGPT** unless Rick says otherwise.

## Modules

Default module path needs explicit init: `module use /soft/modulefiles`

After that, key modules:

- `conda/2025-09-25` (default, alias `conda`) → Python 3.12.11 + torch 2.8.0 (CUDA build), `mconda3` distribution
- `conda/2024-04-29` and several intermediate snapshots if you need an older Python/torch
- `cray-python/3.11.7` (default), `cray-python/3.11.5`
- `cuda/12.9` (default), `cuda/11.8`
- `cpe-cuda/24.11` (default Cray PE for CUDA), several older versions
- `PrgEnv-gnu/8.6.0` (default, replaces nvidia env on conda load)

**NO `apptainer` module** as of 2026-06-13. No `/soft/containers/`. Conda-clone is the path.

## GPU nodes

- 4× NVIDIA A100 40GB per node
- 503GB host RAM
- 64 CPU cores per node
- `pbsnodes -avS | head` to list current node availability

## Queues (`qstat -Q` excerpt, 2026-06-13)

```
debug              17 jobs total,  2 running,  12 held
debug-scaling       7              0           3
prod               85             0            1  (router queue → small/medium/large)
small              28             2           10
medium             37             1           10
large              43             2           31
backfill-small      3              0            0
backfill-medium     2              0            1
backfill-large      1              0            1
preemptable       123             39          32
demand              1              1            0
```

Use `qstat -Qf <queue>` for the full per-queue config (max walltime, max nodes, project gating).

## Conda activation pattern (verified)

```bash
module use /soft/modulefiles
module load conda
conda activate base
which python   # /soft/applications/conda/2025-09-25/mconda3/bin/python
python -c "import torch; print(torch.__version__, torch.version.cuda)"
# torch 2.8.0, cuda 12.x
```

Login nodes have no GPU — `torch.cuda.is_available()` returns False on login. Only test CUDA on compute (interactive: `qsub -I -l select=1 -A AuroraGPT -q debug -l walltime=00:30:00 -l filesystems=eagle:home`).

## Useful one-liners

```bash
# Quota check
ssh cherryrd 'ssh polaris "myprojectquotas | head -30"'

# Project dir listing
ssh cherryrd 'ssh polaris "ls /eagle/projects/AuroraGPT/"'

# Queue snapshot
ssh cherryrd 'ssh polaris "qstat -u stevens"'

# Cancel all my jobs
ssh cherryrd 'ssh polaris "qstat -u stevens | awk \"NR>5 {print \\\$1}\" | xargs -r qdel"'

# Live job log tail
ssh cherryrd 'ssh polaris "tail -f /eagle/projects/AuroraGPT/stevens/<run>/logs/<jobname>.<jobid>"'
```

## OSTI corpus context (2026-06-13)

Staging dir created for the OSTI mass-OCR run:
`/eagle/projects/AuroraGPT/stevens/osti_marker/{pdfs,md,mmd,manifest,logs,scripts,envs,cache}`

Env built at `osti_marker/envs/ocr-py312` (clone of conda/2025-09-25 base + marker-pdf 1.x + nougat-ocr + pymupdf).
