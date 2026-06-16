#!/usr/bin/env python3.13
"""Refresh skill mirrors from canonical ~/.hermes/skills/ into SC-OSTI/skills/.

Run after any skill edit that affects OSTI work. Idempotent.
"""
import shutil
from pathlib import Path

SC = Path("/Users/stevens/Dropbox/SC-OSTI")
SKILLS_ROOT = Path("/Users/stevens/.hermes/skills")

SKILLS = [
    "data-science/corpus-structured-extraction",
    "research/osti-corpus-fetch",
    "hpc/alcf-batch-jobs",
    "devops/dropbox-file-recovery",
]

def main():
    dst_root = SC / "skills"
    for s in SKILLS:
        src = SKILLS_ROOT / s
        if not src.exists():
            print(f"MISSING: {src}")
            continue
        dst = dst_root / Path(s).name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        n = sum(1 for _ in dst.rglob("*") if _.is_file())
        print(f"  refreshed {s}: {n} files")

if __name__ == "__main__":
    main()
