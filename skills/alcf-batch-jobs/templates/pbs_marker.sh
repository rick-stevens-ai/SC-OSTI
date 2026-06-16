#!/bin/bash
#PBS -N osti-marker
#PBS -l select=10:system=polaris
#PBS -l place=scatter
#PBS -l walltime=08:00:00
#PBS -l filesystems=eagle:home
#PBS -q prod
#PBS -A AuroraGPT
#PBS -j oe
#PBS -o /eagle/projects/AuroraGPT/stevens/osti_marker/logs/

# Polaris Marker OCR batch — SQLite work-queue pattern.
# Each MPI rank = one worker = one GPU. Workers atomically claim rows from manifest.sqlite,
# run Marker, write .md atomically, mark row done. Restart = free (skips done rows).
#
# Submit:
#   cd /eagle/projects/AuroraGPT/stevens/osti_marker/scripts
#   qsub pbs_marker.sh
#
# Resubmit on walltime-out: same script — manifest preserves progress.

set -euo pipefail

PROJECT_DIR=/eagle/projects/AuroraGPT/stevens/osti_marker
ENV_PATH=${PROJECT_DIR}/envs/ocr-py312
MANIFEST=${PROJECT_DIR}/manifest/marker_queue.sqlite

cd "$PROJECT_DIR"

# --- Env ---
module use /soft/modulefiles
module load conda
conda activate "$ENV_PATH"

export HF_HOME=${PROJECT_DIR}/cache/huggingface
export TORCH_HOME=${PROJECT_DIR}/cache/torch
export TRANSFORMERS_CACHE=${PROJECT_DIR}/cache/huggingface
# Marker uses surya OCR backend which caches OUTSIDE HF_HOME — MUST set this or
# workers will re-download ~3GB to ~/.cache/datalab on every compute node.
export MODEL_CACHE_DIR=${PROJECT_DIR}/cache/datalab/models
export TOKENIZERS_PARALLELISM=false
# Thread caps — compute nodes have higher limits than login but BLAS thrashing
# across 4 GPU workers per node still hurts. 4-8 is safe; default 64 wastes.
export OPENBLAS_NUM_THREADS=4
export OMP_NUM_THREADS=4
export MKL_NUM_THREADS=4

# --- Compute sizing ---
NODES=$(wc -l < "$PBS_NODEFILE")
GPUS_PER_NODE=4
TOTAL_WORKERS=$((NODES * GPUS_PER_NODE))
echo "Launching $TOTAL_WORKERS workers across $NODES nodes"

# --- Launcher ---
# --ppn = ranks per node ; --depth = CPUs per rank ; --cpu-bind binds CPU set per rank
mpiexec \
  -n $TOTAL_WORKERS \
  --ppn $GPUS_PER_NODE \
  --depth=8 \
  --cpu-bind depth \
  ${PROJECT_DIR}/scripts/marker_worker.sh "$MANIFEST"
