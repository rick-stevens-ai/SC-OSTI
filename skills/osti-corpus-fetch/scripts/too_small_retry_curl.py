#!/usr/bin/env python3
"""
Retry the too_small_* bucket from a Phase C-style state DB using curl subprocess.

Why curl, not urllib: Springer Nature / BMC fingerprint on TLS handshake + HTTP/2
framing + Brotli support. urllib.request with identical headers gets the
3038-byte "cookies_not_supported" stub. curl --compressed gets the real PDF.
See references/urllib-vs-curl-tls-fingerprint-2026-06-10.md for the full
diagnosis.

Tested on cels-rbdgx2 2026-06-10:
  - 1,851 rows from too_small_* buckets in Phase C state DB
  - 6 workers, ~4.3 req/s
  - 62% recovery (1,147 new PDFs in 7 minutes)
  - vs 0% recovery from a urllib-based worker on the same pool

Usage:
  python3 too_small_retry_curl.py \
      --db /rbstor/stevens/unpaywall_overnight.db \
      --out-root /rbstor/stevens/osti_fulltext_unpay \
      --workers 6

Schema expected in the state DB (table 'recovery'):
  osti_id (PK), doi, year, lab, pdf_url, fetch_status, bytes, path, ts

The script:
  1. Selects rows where fetch_status LIKE 'too_small_%' AND pdf_url IS NOT NULL.
  2. Re-fetches each via curl with browser-like headers + cookie jar.
  3. Validates PDF via magic-byte + size check.
  4. Updates fetch_status, bytes, path, ts in place.

Polite by default: 6 workers, 45s timeout per request. No retries — a failure
here means the URL is structurally unreachable, not transient.
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

HEADERS = [
    "-H", "Accept: text/html,application/xhtml+xml,application/xml,application/pdf;q=0.9,*/*;q=0.8",
    "-H", "Accept-Language: en-US,en;q=0.5",
    "-H", "Accept-Encoding: gzip, deflate, br",
]


def validate_pdf(buf):
    """Three-witness PDF validation: magic + size + (caller adds pdftotext check).
    Returns (ok: bool, reason: str)."""
    if not buf:
        return False, "too_small_0"
    if len(buf) < 4096:
        return False, f"too_small_{len(buf)}"
    if buf[:4] != b"%PDF":
        return False, f"not_pdf_magic_{buf[:8].hex()}"
    return True, "ok"


def fetch_one(row, out_root: Path, timeout: int):
    osti_id, doi, year, lab, pdf_url = row
    # Per-fetch temp files for cookies and body
    with tempfile.NamedTemporaryFile(delete=False, suffix=".cookies") as f:
        cookies_file = f.name
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
        body_file = f.name
    try:
        cmd = [
            "curl", "-sL", "--max-time", str(timeout),
            "-A", UA,
            *HEADERS,
            "--compressed",
            "-c", cookies_file, "-b", cookies_file,
            "-o", body_file,
            "-w", "%{http_code}",
            pdf_url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 10)
        http_code = proc.stdout.strip() or "0"
        try:
            buf = open(body_file, "rb").read()
        except OSError:
            buf = b""
        ok, reason = validate_pdf(buf)
        if not ok:
            # If HTTP status was non-200, report that; otherwise report validation reason
            if http_code not in ("200", "0"):
                return osti_id, f"http_{http_code}", len(buf), None
            return osti_id, reason, len(buf), None
        # Write to final location
        year_dir = out_root / (str(year) if year else "unknown")
        year_dir.mkdir(parents=True, exist_ok=True)
        path = year_dir / f"{osti_id}.pdf"
        path.write_bytes(buf)
        return osti_id, "ok", len(buf), str(path)
    except subprocess.TimeoutExpired:
        return osti_id, "err_timeout", 0, None
    except Exception as e:
        return osti_id, f"err_{type(e).__name__}", 0, None
    finally:
        for f in (cookies_file, body_file):
            try:
                os.unlink(f)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="Path to SQLite state DB")
    ap.add_argument("--out-root", required=True, help="Root dir for PDFs (year subdirs)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--bucket-pattern", default="too_small_%",
                    help="LIKE pattern for fetch_status (default: too_small_%%)")
    ap.add_argument("--limit", type=int, default=0, help="Cap on rows (0=all)")
    args = ap.parse_args()

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(args.db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.execute(
        "SELECT osti_id, doi, year, lab, pdf_url FROM recovery "
        "WHERE fetch_status LIKE ? AND pdf_url IS NOT NULL "
        + ("LIMIT ?" if args.limit else ""),
        ((args.bucket_pattern, args.limit) if args.limit else (args.bucket_pattern,)),
    )
    targets = cur.fetchall()
    con.close()
    total = len(targets)
    print(f"[start] {total} rows matching '{args.bucket_pattern}' to retry "
          f"via curl ({args.workers} workers)", flush=True)
    if total == 0:
        return

    counts = {"ok": 0, "fail": 0}
    started = time.time()
    write_lock = threading.Lock()

    def commit_result(r):
        oid, status, sz, path = r
        with write_lock:
            con2 = sqlite3.connect(args.db, timeout=60)
            con2.execute(
                "UPDATE recovery SET fetch_status=?, bytes=?, path=?, ts=? "
                "WHERE osti_id=?",
                (status, sz, path, int(time.time()), oid))
            con2.commit()
            con2.close()
        if status == "ok":
            counts["ok"] += 1
        else:
            counts["fail"] += 1

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_one, t, out_root, args.timeout): t[0]
                   for t in targets}
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                commit_result(fut.result())
            except Exception as e:
                print(f"  worker exc: {e}", flush=True)
            if done % 25 == 0:
                rate = done / (time.time() - started)
                eta_min = (total - done) / rate / 60 if rate > 0 else 0
                print(f"  done={done}/{total} ok={counts['ok']} "
                      f"fail={counts['fail']} rate={rate:.1f}/s "
                      f"eta={eta_min:.0f}min", flush=True)

    elapsed = time.time() - started
    print(f"FINAL ok={counts['ok']} fail={counts['fail']} "
          f"total={total} elapsed={elapsed:.0f}s "
          f"recovery_pct={100.0*counts['ok']/total:.1f}%", flush=True)


if __name__ == "__main__":
    main()
