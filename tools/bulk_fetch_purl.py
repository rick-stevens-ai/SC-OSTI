#!/usr/bin/env python3
"""Bulk PURL fetcher for OSTI papers missing local PDFs.

Args:
  --year-start / --year-end : inclusive range
  --delay                   : seconds between requests (default 0.8)
  --workers                 : concurrent fetchers (default 1; raise carefully)
  --limit                   : cap to first N (smoke testing)
  --target-dir              : where to write PDFs (default /Volumes/SG-1-8TB/osti/pdfs)

Writes:
  <target-dir>/<year>/<osti_id>.pdf
  pdf_fetch_log row per attempt (osti_id, url, http_status, bytes, sha256, target_path, ts, error)
  Updates papers.has_pdf=1 and inserts file_instances row on success.

Retry policy: 503, timeout, exception -> exp backoff up to 3 attempts.
Skips osti_id if file already exists at target path (resumable).
"""
import sqlite3, os, sys, argparse, time, hashlib, urllib.request, urllib.error, ssl, random
from pathlib import Path
from datetime import datetime, timezone

DB = "/Volumes/SG-1-8TB/osti/catalog/catalog.sqlite"
UA = "Mozilla/5.0 (Kukla agent / osti-corpus-backfill; rick.stevens.ai@gmail.com)"
TIMEOUT = 60
MAX_RETRY = 3

def fetch(osti_id, target_path, delay):
    """Returns (bucket, http_status, bytes, sha256, error_msg)."""
    url = f"https://www.osti.gov/servlets/purl/{osti_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"})
    ctx = ssl.create_default_context()
    last_err = ""
    for attempt in range(MAX_RETRY):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                ctype = r.headers.get("Content-Type", "")
                head = r.read(8192)
                if not head.startswith(b"%PDF") and "pdf" not in ctype.lower():
                    final_url = r.geturl()
                    if final_url != url:
                        return ("redirect_off", 200, 0, "", f"redirected to {final_url[:200]}")
                    return ("wrong_type", 200, 0, "", f"ctype={ctype}")
                # Stream rest to disk
                tmp = target_path.with_suffix(target_path.suffix + ".part")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                total = 0
                with open(tmp, "wb") as out:
                    out.write(head); h.update(head); total += len(head)
                    while True:
                        chunk = r.read(65536)
                        if not chunk: break
                        out.write(chunk); h.update(chunk); total += len(chunk)
                if total <= 1024:
                    tmp.unlink(missing_ok=True)
                    return ("empty", 200, total, "", "size<=1024")
                tmp.rename(target_path)
                return ("recovered_pdf", 200, total, h.hexdigest(), "")
        except urllib.error.HTTPError as e:
            if e.code in (503, 429) and attempt < MAX_RETRY - 1:
                time.sleep((2 ** attempt) * delay + random.random())
                continue
            return (f"http_{e.code}", e.code, 0, "", str(e)[:200])
        except urllib.error.URLError as e:
            reason = str(e.reason).lower()
            if ("timed out" in reason or "timeout" in reason) and attempt < MAX_RETRY - 1:
                time.sleep((2 ** attempt) * delay + random.random())
                last_err = reason
                continue
            return ("timeout" if "timeout" in reason else "exception", 0, 0, "", str(e)[:200])
        except Exception as e:
            return ("exception", 0, 0, "", str(e)[:200])
    return ("timeout", 0, 0, "", last_err)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year-start", type=int, required=True)
    ap.add_argument("--year-end", type=int, required=True)
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--target-dir", default="/Volumes/SG-1-8TB/osti/pdfs")
    ap.add_argument("--run-tag", default="backfill_v1")
    args = ap.parse_args()

    target_root = Path(args.target_dir)
    target_root.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB, timeout=600)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    # Open a refresh_runs row for this fetch pass
    started_iso = datetime.now(timezone.utc).isoformat()
    import json as _json
    cur.execute("""
        INSERT INTO refresh_runs (run_type, started_ts, params_json, records_added, records_updated, pdfs_added, errors)
        VALUES ('backfill_purl', ?, ?, 0, 0, 0, 0)
    """, (started_iso, _json.dumps({
        "year_start": args.year_start, "year_end": args.year_end,
        "delay": args.delay, "limit": args.limit, "run_tag": args.run_tag,
        "target_dir": str(target_root),
    })))
    run_id = cur.lastrowid
    conn.commit()
    print(f"refresh_runs run_id={run_id}")

    # Pick all papers in year range that don't have a local PDF yet
    sql = """
        SELECT p.osti_id, p.year, p.primary_lab
        FROM papers p
        LEFT JOIN file_instances fi ON p.osti_id = fi.osti_id
        WHERE p.year BETWEEN ? AND ? AND fi.osti_id IS NULL
        ORDER BY p.year, p.osti_id
    """
    params = [args.year_start, args.year_end]
    if args.limit:
        sql += " LIMIT ?"
        params.append(args.limit)
    targets = cur.execute(sql, params).fetchall()
    print(f"Picked {len(targets):,} papers in {args.year_start}-{args.year_end} missing PDFs")
    print(f"Delay={args.delay}s, target_dir={target_root}")

    # Track stats
    counts = {}
    started_ts = time.time()
    t0 = started_ts

    for i, (osti_id, year, lab) in enumerate(targets, 1):
        target_path = target_root / str(year) / f"{osti_id}.pdf"
        # Skip if already on disk
        if target_path.exists():
            counts["already_present"] = counts.get("already_present", 0) + 1
            continue

        bucket, status, nbytes, sha, err = fetch(osti_id, target_path, args.delay)
        counts[bucket] = counts.get(bucket, 0) + 1

        # Log every attempt — separate try blocks so log failure doesn't block file_instances update
        ts = datetime.now(timezone.utc).isoformat()
        url = f"https://www.osti.gov/servlets/purl/{osti_id}"
        try:
            cur.execute("""
                INSERT INTO pdf_fetch_log (osti_id, run_id, ts, url, http_status, bytes, sha256, saved_path, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (osti_id, run_id, ts, url, status, nbytes, sha,
                  str(target_path) if bucket == "recovered_pdf" else "", err[:500]))
        except Exception as e:
            print(f"  log error {osti_id}: {e}", file=sys.stderr)

        # On success, also UPSERT file_instances and update papers
        if bucket == "recovered_pdf":
            try:
                cur.execute("""
                    INSERT OR IGNORE INTO file_instances
                        (osti_id, source, path, size, sha256, is_canonical, first_seen_ts, last_verified_ts)
                    VALUES (?, 'backfill_purl', ?, ?, ?, 1, ?, ?)
                """, (osti_id, str(target_path), nbytes, sha, ts, ts))
                cur.execute("""
                    UPDATE papers SET has_pdf=1, canonical_pdf_path=?, canonical_source='backfill_purl',
                                      canonical_size=?, canonical_sha256=?
                    WHERE osti_id=?
                """, (str(target_path), nbytes, sha, osti_id))
            except Exception as e:
                print(f"  upsert error {osti_id}: {e}", file=sys.stderr)
        else:
            # Enqueue for DOI-based recovery — every non-recovered, non-already-present outcome
            try:
                cur.execute("""
                    INSERT OR IGNORE INTO recovery_queue
                        (osti_id, reason, enqueued_ts, enqueued_run_id, status)
                    VALUES (?, ?, ?, ?, 'pending')
                """, (osti_id, bucket, ts, run_id))
            except Exception as e:
                print(f"  enqueue error {osti_id}: {e}", file=sys.stderr)

        if i % 50 == 0:
            conn.commit()

        # Progress
        if i % 100 == 0 or i == len(targets):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(targets) - i) / rate / 60 if rate > 0 else 0
            print(f"  {i}/{len(targets)} rate={rate:.1f}/s eta={eta:.0f}min counts={counts}", flush=True)

        time.sleep(args.delay)

    conn.commit()
    ended_iso = datetime.now(timezone.utc).isoformat()
    pdfs_added = counts.get("recovered_pdf", 0)
    errors = sum(v for k, v in counts.items() if k not in ("recovered_pdf", "already_present"))
    cur.execute("""
        UPDATE refresh_runs SET ended_ts=?, pdfs_added=?, errors=?, notes=?
        WHERE run_id=?
    """, (ended_iso, pdfs_added, errors, _json.dumps(counts), run_id))
    conn.commit()
    conn.close()

    elapsed = (time.time() - started_ts) / 60
    print(f"\n=== Done in {elapsed:.1f}min ===")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:25s} {v:>6,}")

if __name__ == "__main__":
    main()
