#!/bin/bash
# Re-runnable Polaris environment probe.
# Run from m1 (or anywhere with cherryrd ssh). Prints the facts needed to plan a batch submission.
# Refresh references/polaris-quick-facts.md from the output if anything has changed materially.
#
# Pre-req: Rick has parked an MFA ControlMaster socket via interactive `ssh polaris` from cherryrd.

set -euo pipefail

REMOTE='ssh cherryrd "ssh polaris"'

echo "=== HOST ==="
ssh cherryrd 'ssh polaris "hostname; whoami; date"' 2>&1 | grep -v "post-quantum"

echo ""
echo "=== FILESYSTEMS ==="
ssh cherryrd 'ssh polaris "df -h /eagle /grand /home 2>/dev/null | head -10"' 2>&1 | grep -v "post-quantum"

echo ""
echo "=== MY ALLOCATIONS ==="
ssh cherryrd 'ssh polaris "myprojectquotas"' 2>&1 | grep -v "post-quantum"

echo ""
echo "=== MY EAGLE PROJECT DIRS ==="
ssh cherryrd 'ssh polaris "ls /eagle/projects/ 2>/dev/null | head -40"' 2>&1 | grep -v "post-quantum"

echo ""
echo "=== KEY MODULES ==="
ssh cherryrd 'ssh polaris "module use /soft/modulefiles && module avail 2>&1 | grep -iE \"(conda|apptainer|cuda|python|pytorch)\" | head -30"' 2>&1 | grep -v "post-quantum"

echo ""
echo "=== QUEUES ==="
ssh cherryrd 'ssh polaris "qstat -Q 2>/dev/null | head -20"' 2>&1 | grep -v "post-quantum"

echo ""
echo "=== GPU NODE SAMPLE ==="
ssh cherryrd 'ssh polaris "pbsnodes -avS 2>/dev/null | head -5"' 2>&1 | grep -v "post-quantum"

echo ""
echo "=== MY RUNNING/QUEUED JOBS ==="
ssh cherryrd 'ssh polaris "qstat -u stevens 2>&1 | head -20"' 2>&1 | grep -v "post-quantum"

echo ""
echo "=== CONDA ACTIVATION TEST ==="
ssh cherryrd 'ssh polaris "module use /soft/modulefiles && module load conda 2>/dev/null && conda activate base && which python && python -c \"import torch; print(\\\"torch=\\\", torch.__version__, \\\"cuda_built=\\\", torch.version.cuda)\""' 2>&1 | grep -v "post-quantum"

echo ""
echo "=== Probe complete ==="
