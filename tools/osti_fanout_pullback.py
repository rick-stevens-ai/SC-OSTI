#!/usr/bin/env python3
"""
osti_fanout_pullback.py — keep SG-1-8TB mirror current + pull parsed output home.

Runs on m1-mac-mini (only host that mounts SG-1-8TB). Idempotent; safe to cron.

Stages:
  C1. DGX canonical PDFs  -> SG-1-8TB/osti/canonical_flat/   (incremental rsync)
  C2. DGX parsed .md      -> SG-1-8TB/osti/md_flat/          (incremental rsync)
  D1. Polaris parsed .md  -> SG-1-8TB/osti/md_flat/          (two-stage relay via cherryrd)
  E.  Catalog md_path update for any new .md landed locally

Does NOT submit jobs, touch Polaris manifests, or touch the nougat lane.
That's handled separately (and nougat is Ollie's lane).

Network notes:
  - DGX (cels-rbdgx2) reachable directly from m1.
  - Polaris reached only via cherryrd two-hop; m1 has no polaris host key, so the
    pullback uses stage1 (polaris->cherryrd:/tmp) + stage2 (cherryrd->m1).
  - rsync MUST be /opt/homebrew/bin/rsync on macOS (NOT /usr/bin/rsync).
"""
from __future__ import annotations
import subprocess, sqlite3, sys, os, glob, time
from datetime import datetime, timezone
from pathlib import Path

RSYNC = "/opt/homebrew/bin/rsync"
SG = Path("/Volumes/SG-1-8TB/osti")
CANON = SG / "canonical_flat"
MDFLAT = SG / "md_flat"
DB = SG / "catalog" / "catalog.sqlite"

DGX = "cels-rbdgx2"
DGX_CANON = "/rbstor/stevens/osti_canonical_flat/"
DGX_MD = "/rbstor/stevens/osti_fulltext_v2_md/"

CHERRY = "cherryrd"
POLARIS_MD_EAGLE = "/eagle/projects/AuroraGPT/stevens/osti_marker/md/prod_mirror/"
RELAY = "/tmp/kukla_polaris_md_relay"

LOG = SG / "logs" / "fanout_pullback.log"


def log(msg: str):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def sh(cmd: list[str], timeout=3600) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def preflight() -> bool:
    if not SG.exists():
        log(f"[abort] SG-1-8TB not mounted ({SG})"); return False
    if not Path(RSYNC).exists():
        log(f"[abort] rsync not found at {RSYNC}"); return False
    CANON.mkdir(parents=True, exist_ok=True)
    MDFLAT.mkdir(parents=True, exist_ok=True)
    return True


def stage_c1_pdfs():
    log("C1: DGX canonical PDFs -> SG-1-8TB (incremental)")
    rc, out, err = sh([RSYNC, "-a", "--info=stats2", f"{DGX}:{DGX_CANON}", f"{CANON}/"])
    tail = (out.strip().splitlines() or ["(no stats)"])[-6:]
    log("C1 " + ("OK" if rc == 0 else f"rc={rc}") + " | " + " ".join(t.strip() for t in tail if t.strip()))


def stage_c2_md():
    log("C2: DGX parsed .md -> SG-1-8TB (incremental)")
    rc, out, err = sh([RSYNC, "-a", "--info=stats2", f"{DGX}:{DGX_MD}", f"{MDFLAT}/"])
    tail = (out.strip().splitlines() or ["(no stats)"])[-4:]
    log("C2 " + ("OK" if rc == 0 else f"rc={rc}") + " | " + " ".join(t.strip() for t in tail if t.strip()))


def pick_polaris_login() -> str | None:
    for n in ("01", "02", "04", "03"):
        rc, out, _ = sh(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", CHERRY,
                         f"ssh -o ConnectTimeout=10 -o BatchMode=yes polaris-{n} hostname"], timeout=40)
        if rc == 0 and out.strip():
            return f"polaris-{n}"
    return None


def stage_d1_polaris_pullback():
    log("D1: Polaris parsed .md -> SG-1-8TB (two-stage relay)")
    node = pick_polaris_login()
    if not node:
        log("D1 [skip] no live polaris login node"); return
    log(f"D1 using {node}")
    # stage1: polaris -> cherryrd:/tmp relay (run on cherryrd)
    rc, out, err = sh(["ssh", "-o", "ConnectTimeout=12", "-o", "BatchMode=yes", CHERRY,
                       f"mkdir -p {RELAY} && rsync -a {node}:{POLARIS_MD_EAGLE} {RELAY}/ 2>&1 | tail -2; "
                       f"echo RELAY_COUNT=$(ls {RELAY}/*.md 2>/dev/null | wc -l)"], timeout=900)
    log("D1 stage1 " + ("OK" if rc == 0 else f"rc={rc}") + " | " + out.strip().replace("\n", " ")[:200])
    # stage2: cherryrd:/tmp relay -> m1 md_flat
    # cherryrd only has openrsync (protocol 29, no --info); use -v --stats instead.
    rc, out, err = sh([RSYNC, "-a", "--stats", f"{CHERRY}:{RELAY}/", f"{MDFLAT}/"], timeout=900)
    if rc != 0:
        # openrsync compatibility fallback: bare -a, no stats flags at all
        rc, out, err = sh([RSYNC, "-a", f"{CHERRY}:{RELAY}/", f"{MDFLAT}/"], timeout=900)
    n_md = len(glob.glob(f"{MDFLAT}/*.md"))
    log("D1 stage2 " + ("OK" if rc == 0 else f"rc={rc} err={err.strip()[:160]}") + f" | md_flat now {n_md} .md")


def stage_e_catalog_update():
    log("E: catalog md_path update")
    if not DB.exists():
        log("E [skip] catalog not present"); return
    md_ids = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(f"{MDFLAT}/*.md")}
    con = sqlite3.connect(str(DB), timeout=60, isolation_level=None)
    cur = con.cursor()
    cur.execute("PRAGMA busy_timeout=60000")
    if cur.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
        cur.execute("PRAGMA journal_mode=WAL")
    updated = 0
    for oid in md_ids:
        cur.execute("UPDATE papers SET md_path=? WHERE osti_id=? AND (md_path IS NULL OR md_path='')",
                    (f"md_flat/{oid}.md", oid))
        updated += cur.rowcount
    con.commit()
    md_set = cur.execute("SELECT COUNT(*) FROM papers WHERE md_path IS NOT NULL AND md_path!=''").fetchone()[0]
    con.close()
    log(f"E OK | md files on disk={len(md_ids)} new md_path set={updated} total md_set={md_set}")


def main() -> int:
    log("=== fanout+pullback run start ===")
    if not preflight():
        return 1
    stage_c1_pdfs()
    stage_c2_md()
    stage_d1_polaris_pullback()
    stage_e_catalog_update()
    log("=== run complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
