#!/usr/bin/env python3
"""
osti_harvest.py — incremental OSTI metadata harvest + PDF fetch, run from m1.

The catalog (single source of truth) lives on SG-1-8TB (m1-only mount), but m1's
Comcast IP blackholes www.osti.gov. This launcher routes all OSTI HTTP egress
through a SOCKS proxy tunneled via cels-rbdgx2 (which has a clean OSTI path),
so the harvest tools run UNMODIFIED on m1 against the local catalog — no DB copy,
no merge-back, no divergence.

Stages:
  A. fetch_osti_catalog.py --since-days N   (incremental metadata UPSERT)
  B. bulk_fetch_purl.py --year-start --year-end  (PDF fetch for new no-PDF papers)
     -> writes PDFs to DGX canonical dir? No: writes to SG-1-8TB/osti/pdfs by default.
        The fan-out cron then replicates m1->... ; but canonical master is DGX.
        For new fetches we write to SG-1-8TB/osti/pdfs/<year>/ and let a follow-up
        push them to DGX (durability: m1 + DGX). See --push-dgx.

Usage:
  osti_harvest.py --since-days 8 [--smoke] [--fetch-pdfs] [--push-dgx]
  --smoke      : metadata harvest capped tiny + a 10-PDF fetch, for validation
  --fetch-pdfs : run stage B (default: metadata only)
  --push-dgx   : after fetch, rsync new PDFs SG-1-8TB -> DGX canonical (durability)

Network: opens `ssh -D <port> -N cels-rbdgx2` if not already up; installs a global
SOCKS5 opener into urllib so the child tools' urllib calls exit via cels.
"""
from __future__ import annotations
import argparse, subprocess, sys, time, socket, os
from datetime import datetime, timezone
from pathlib import Path

SC_TOOLS = Path("/Users/stevens/Dropbox/SC-OSTI/tools")
VENV_PY = "/Users/stevens/.hermes/ocr-mcp-venv/bin/python"  # has PySocks
SOCKS_PORT = 18080
CELS = "cels-rbdgx2"
LOG = Path("/Volumes/SG-1-8TB/osti/logs/harvest.log")


def log(msg: str):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def socks_up(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def ensure_tunnel():
    if socks_up(SOCKS_PORT):
        log(f"SOCKS proxy already up on :{SOCKS_PORT}")
        return
    log(f"opening SOCKS proxy :{SOCKS_PORT} via {CELS}")
    subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-D", str(SOCKS_PORT), "-N", "-f", CELS],
                   timeout=30)
    for _ in range(10):
        if socks_up(SOCKS_PORT):
            log("SOCKS proxy is up"); return
        time.sleep(1)
    raise RuntimeError("failed to bring up SOCKS proxy via cels")


# The child tools use urllib directly. We can't edit them, so we inject a
# sitecustomize that installs a SOCKS opener, via PYTHONSTARTUP-like shim:
# easiest is to run them through a wrapper that monkeypatches socket before import.
WRAPPER = r'''
import sys, socks, socket, runpy
socks.set_default_proxy(socks.SOCKS5, "127.0.0.1", {port})
socket.socket = socks.socksocket
# run the target module as __main__ with the remaining argv
target = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(target, run_name="__main__")
'''


def run_tool(tool: str, args: list[str]):
    wrapper_path = Path("/tmp/_osti_socks_wrapper.py")
    wrapper_path.write_text(WRAPPER.format(port=SOCKS_PORT))
    cmd = [VENV_PY, str(wrapper_path), str(SC_TOOLS / tool)] + args
    log(f"RUN {tool} {' '.join(args)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=86400)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    # echo last lines
    for ln in out.splitlines()[-12:]:
        log(f"  {tool}| {ln}")
    if err:
        for ln in err.splitlines()[-6:]:
            log(f"  {tool}!! {ln}")
    log(f"{tool} exit={r.returncode}")
    return r.returncode


def push_dgx():
    """Replicate newly-fetched PDFs SG-1-8TB -> DGX canonical (durability)."""
    log("push-dgx: SG-1-8TB/osti/pdfs -> DGX (additive, no delete)")
    # DGX canonical is flat by osti_id; the new fetches are year-sharded. Push the
    # year-sharded tree to a parallel dir on DGX so nothing clobbers canonical_flat.
    rc = subprocess.run([
        "/opt/homebrew/bin/rsync", "-a", "--ignore-existing",
        "/Volumes/SG-1-8TB/osti/pdfs/", f"{CELS}:/rbstor/stevens/osti_fulltext_v3_incremental/"
    ], timeout=7200).returncode
    log(f"push-dgx exit={rc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-days", type=int, default=8)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--fetch-pdfs", action="store_true")
    ap.add_argument("--push-dgx", action="store_true")
    args = ap.parse_args()

    log(f"=== harvest start (since-days={args.since_days} smoke={args.smoke} "
        f"fetch_pdfs={args.fetch_pdfs}) ===")
    ensure_tunnel()

    # Stage A: incremental metadata
    a_args = ["--since-days", str(args.since_days), "--run-type",
              "incremental_smoke" if args.smoke else "incremental_weekly"]
    rc_a = run_tool("fetch_osti_catalog.py", a_args)

    # Stage B: PDF fetch for recent no-PDF papers (current + prior year)
    if args.fetch_pdfs:
        yr = datetime.now(timezone.utc).year
        b_args = ["--year-start", str(yr - 1), "--year-end", str(yr), "--delay", "0.8",
                  "--run-tag", "incremental_purl"]
        if args.smoke:
            b_args += ["--limit", "10"]
        rc_b = run_tool("bulk_fetch_purl.py", b_args)
        if rc_b == 0 and args.push_dgx:
            push_dgx()

    log("=== harvest complete ===")
    return rc_a


if __name__ == "__main__":
    sys.exit(main())
