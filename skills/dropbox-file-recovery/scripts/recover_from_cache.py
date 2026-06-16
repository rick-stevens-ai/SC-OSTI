#!/usr/bin/env python3
"""Recover lost files from Dropbox's content-addressed cache.

USAGE
-----
1. Edit NEEDLES below to be distinctive byte-strings unique to your lost files
   (class names, function names, distinctive docstrings — NOT generic words).
2. Edit SIZE_RANGE if your files are very small or very large.
3. Run: python3 recover_from_cache.py
4. Review the printed hits.
5. Build RESTORE_MAP (hash -> intended path) from the hits + verbatim
   file shapes you remember.
6. Uncomment the restore section and re-run.

Why not shell `grep -l`? With ~20K+ cache files, the shell glob expansion
plus per-file grep startup overhead reliably hangs past 60s.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# -------- EDIT THESE --------

CACHE_DIR = Path("~/Dropbox/.dropbox.cache/old_files").expanduser()

# Repository root where files should be restored
REPO_ROOT = Path("~/Dropbox/OLLIE/my-repo").expanduser()

# Distinctive byte-strings unique to your lost files. The more specific
# the better — "MultiSearch" is great, "import" is useless.
NEEDLES = [
    b"MultiSearch",
    b"IndexBuilder",
    b"search_hybrid",
    # Add project-specific identifiers
]

# Size pre-filter (bytes). Most source files are 500-50,000. Adjust for
# big data files or tiny configs.
SIZE_RANGE = (500, 50_000)

# After the FIND phase identifies hits, fill this map and re-run with
# RESTORE=True. Key = cache hash, Value = path under REPO_ROOT.
RESTORE_MAP: dict[str, str] = {
    # "918ead4ead09e9d57175b9654f5f7359": "examples/multimodal/01_build_index.py",
}

RESTORE = False  # Set True after you've built RESTORE_MAP

# -------- DON'T EDIT BELOW --------


def find_phase() -> None:
    """Scan cache for files containing any NEEDLE, print hits with previews."""
    if not CACHE_DIR.exists():
        sys.exit(f"Cache dir not found: {CACHE_DIR}")

    files = list(CACHE_DIR.iterdir())
    print(f"Total cache files: {len(files)}")

    lo, hi = SIZE_RANGE
    candidates = [(f, f.stat().st_size) for f in files
                  if f.is_file() and lo <= f.stat().st_size <= hi]
    print(f"In size range [{lo}, {hi}]: {len(candidates)}")

    hits: list[tuple[Path, int, str, bytes]] = []
    for fp, sz in candidates:
        try:
            data = fp.read_bytes()
        except OSError:
            continue
        for needle in NEEDLES:
            if needle in data:
                preview = data[:300].decode("utf-8", "replace").replace("\n", " ")
                hits.append((fp, sz, needle.decode(), data))
                break

    print(f"Hits: {len(hits)}\n")
    for fp, sz, needle, data in hits:
        preview = data[:200].decode("utf-8", "replace")
        print(f"=== {fp.name}  ({sz}b)  [{needle}]")
        print(preview)
        print()


def restore_phase() -> None:
    """Copy files from cache to REPO_ROOT per RESTORE_MAP."""
    if not RESTORE_MAP:
        sys.exit("RESTORE_MAP is empty — nothing to restore")

    restored = []
    for cache_hash, rel_path in RESTORE_MAP.items():
        src = CACHE_DIR / cache_hash
        dst = REPO_ROOT / rel_path
        if not src.exists():
            print(f"MISSING in cache: {cache_hash} -> {rel_path}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored.append((rel_path, dst.stat().st_size))

    print(f"Restored {len(restored)} files:")
    for path, sz in restored:
        print(f"  {sz:7d}b  {path}")


if __name__ == "__main__":
    if RESTORE:
        restore_phase()
    else:
        find_phase()
