#!/usr/bin/env /opt/homebrew/bin/python3.13
"""Build a stratified-by-lab sample of OSTI IDs from a failed-recovery list.

Reads:  ~/code/osti-replication-candidates/failed_recovery.txt   (one OSTI ID per line)
Reads:  ~/code/osti-replication-candidates/recon_v2/*.jsonl      (per-lab/year shards
                                                                   with osti_id, doi, title, links)
Writes: ~/code/osti-replication-candidates/sample_50_for_cels_probe.tsv

Output columns (tab-separated):
    osti_id  lab  year  doi  title  purl

Why stratify by lab: in 2026-06-08 probing, failure pattern clustered by lab
(PNNL/LBNL/Fermi/JLab = 0% recovery, mostly 403; Argonne/SLAC/PPPL/BNL =
60-100% recovery). A random sample would over-weight whichever lab dominates
the failed_recovery.txt count; stratified sampling forces visibility into
the per-lab failure regime.

Tune `target_per_lab` if you want a wider/narrower sweep.
"""
from pathlib import Path
import json
import random
import re
from collections import defaultdict

ROOT = Path.home() / "code/osti-replication-candidates"
FAILED = ROOT / "failed_recovery.txt"
RECON = ROOT / "recon_v2"
OUT = ROOT / "sample_50_for_cels_probe.tsv"
TARGET_PER_LAB = 5  # 10 labs × 5 = ~50 sample

failed_ids = set(line.strip() for line in FAILED.read_text().splitlines() if line.strip())
print(f"failed IDs total: {len(failed_ids):,}")

id_to_meta = {}
for shard in sorted(RECON.glob("*.jsonl")):
    m = re.match(r"(.+?)__(\d{4})\.jsonl$", shard.name)
    if not m:
        continue
    lab, year = m.group(1), int(m.group(2))
    for line in shard.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        oid = str(r.get("osti_id") or r.get("id") or "")
        if oid and oid in failed_ids:
            id_to_meta[oid] = (lab, year, r)

print(f"failed IDs matched in recon_v2: {len(id_to_meta):,}")
unmatched = failed_ids - set(id_to_meta.keys())
print(f"failed IDs NOT in recon_v2: {len(unmatched):,}  <- worth flagging separately")

by_lab = defaultdict(list)
for oid, (lab, year, rec) in id_to_meta.items():
    by_lab[lab].append((oid, year, rec))

random.seed(42)
sample = []
for lab in sorted(by_lab):
    pool = by_lab[lab]
    random.shuffle(pool)
    chunk = pool[:TARGET_PER_LAB]
    sample.extend((lab, *t) for t in chunk)

print(f"sample size: {len(sample)} from {len(by_lab)} labs (~{TARGET_PER_LAB}/lab)")

with OUT.open("w") as f:
    f.write("osti_id\tlab\tyear\tdoi\ttitle\tpurl\n")
    for lab, oid, year, rec in sample:
        doi = rec.get("doi", "") or ""
        title = (rec.get("title", "") or "").replace("\t", " ").replace("\n", " ")[:80]
        links = rec.get("links", []) or []
        purl = next((L.get("href", "") for L in links if "purl" in L.get("href", "").lower()), "")
        if not purl:
            purl = f"https://www.osti.gov/servlets/purl/{oid}"
        f.write(f"{oid}\t{lab}\t{year}\t{doi}\t{title}\t{purl}\n")

print(f"wrote {OUT}")
