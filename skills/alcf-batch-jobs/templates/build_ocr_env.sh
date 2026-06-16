#!/bin/bash
# Build a project-local OCR conda env on Polaris /eagle.
# Pattern: clone the system base env (preserves torch+CUDA+MPI wiring) then layer marker-pdf + nougat-ocr.
#
# Run on a Polaris LOGIN NODE (polaris-login-04), not from cherryrd or m1.
# Backgrounded for long runs (~10-15 min for clone + pip install + model pre-warm):
#   nohup bash build_ocr_env.sh > logs/build_env.log 2>&1 & disown
#
# After build, smoke-test on an interactive compute node:
#   qsub -I -l select=1 -A AuroraGPT -q debug -l walltime=00:30:00 -l filesystems=eagle:home
#   conda activate /eagle/projects/AuroraGPT/stevens/osti_marker/envs/ocr-py312
#   python -c "import torch; print(torch.cuda.is_available())"   # must be True on compute

set -euo pipefail

# --- TWEAK FOR YOUR PROJECT ---
PROJECT=AuroraGPT
USER_DIR=/eagle/projects/${PROJECT}/stevens/osti_marker
ENV_PATH=${USER_DIR}/envs/ocr-py312
CACHE_DIR=${USER_DIR}/cache

# --- Login-node thread caps (REQUIRED, see pitfalls) ---
# Polaris login nodes cap user threads; default OpenBLAS spins 64 threads and
# crashes numpy import with "Resource temporarily unavailable". Cap to 2 here;
# compute-node workers can override higher in their PBS env.
export OPENBLAS_NUM_THREADS=2
export OMP_NUM_THREADS=2
export MKL_NUM_THREADS=2

# --- Module env ---
module use /soft/modulefiles
module load conda
conda activate base

# --- Clone base ---
if [ ! -d "$ENV_PATH" ]; then
  echo "=== Cloning base env to $ENV_PATH ==="
  conda create --clone base -p "$ENV_PATH" -y
fi

conda activate "$ENV_PATH"
echo "Active env: $CONDA_PREFIX"
echo "Python: $(which python) -- $(python --version)"
python -c "import torch; print('torch=', torch.__version__, 'cuda_built=', torch.version.cuda)"

# --- Cache dirs on /eagle so all compute nodes share + survive across jobs ---
mkdir -p "$CACHE_DIR/huggingface" "$CACHE_DIR/torch" "$CACHE_DIR/datalab/models"
export HF_HOME="$CACHE_DIR/huggingface"
export TORCH_HOME="$CACHE_DIR/torch"
export TRANSFORMERS_CACHE="$CACHE_DIR/huggingface"
# Marker uses surya OCR backend which caches OUTSIDE HF_HOME — must set MODEL_CACHE_DIR
# explicitly. Without this, ~3GB of surya weights (layout, text_detection,
# text_recognition, table_recognition) land in ~/.cache/datalab/ instead of /eagle.
export MODEL_CACHE_DIR="$CACHE_DIR/datalab/models"

# --- Install OCR stack ---
pip install --no-input \
  "marker-pdf>=1.0.0,<2.0.0" \
  "nougat-ocr" \
  "pymupdf"

# --- Smoke imports ---
echo "=== Smoke import test ==="
python -c "
import marker
from marker.converters.pdf import PdfConverter
print('marker version:', marker.__version__)
print('PdfConverter import OK')
"

# --- Pre-warm Marker models (downloads ~3-5GB to /eagle cache) ---
echo "=== Pre-warming Marker model cache ==="
python -c "
from marker.models import create_model_dict
md = create_model_dict()
print('models loaded:', list(md.keys()))
"

# --- Verify both cache locations landed on /eagle (catches MODEL_CACHE_DIR misses) ---
echo "=== Cache landing check ==="
echo "HF cache:      $(du -sh $CACHE_DIR/huggingface 2>/dev/null | cut -f1)"
echo "Datalab cache: $(du -sh $CACHE_DIR/datalab 2>/dev/null | cut -f1)"
echo "Home leak:     $(du -sh ~/.cache/datalab 2>/dev/null | cut -f1 || echo 'none')"
# If "Home leak" is non-zero, MODEL_CACHE_DIR didn't take — re-check spelling/export.

echo "=== DONE ==="
du -sh "$ENV_PATH" "$CACHE_DIR"
echo ""
echo "Next: copy templates/pbs_marker.sh into ${USER_DIR}/scripts/ and edit project + paths."
echo "REMINDER: worker PBS scripts MUST also export MODEL_CACHE_DIR=$CACHE_DIR/datalab/models"
echo "          and OPENBLAS_NUM_THREADS=2 / OMP_NUM_THREADS=2 / MKL_NUM_THREADS=2"
