#!/usr/bin/env python3
"""Bulk PURL fetcher for OSTI papers missing local PDFs — catalog-aware.

Pattern proved out 2026-06-13 on the 2000-2005 backfill (24,945 IDs queued
after smoke). Three things this template gets right that ad-hoc fetchers
usually miss:

  1. SCHEMA INTROSPECTION at script init — prints actual columns for every
     table the script will INSERT into, so a schema drift between the
     handoff summary and reality fails LOUD at second 0 instead of silently
     at row 1, 50, 100, ...

  2. SEPARATE TRY BLOCKS for the fetch_log INSERT vs the file_instances /
     papers UPSERT. A column-name error on the log shouldn't roll back the
     canonical-PDF state update for the next 49 successful fetches.

  3. REFRESH_RUNS LIFECYCLE — opens a row at start with started_ts +
     params_json, closes it at end with ended_ts + pdfs_added + errors +
     notes (final counts as JSON). Audit trail survives the script exit.

Resume-safe via target_path.exists() check (skips already-fetched PDFs
without re-hitting OSTI). Pair with reconcile_pdfs.py if a prior run wrote
files to disk but DB updates failed.

Args:
  --year-start / --year-end : inclusive range
  --delay                   : seconds between requests (default 0.8)
  --limit                   : cap to first N (smoke testing)
  --target-dir              : where to write PDFs (default <catalog_root>/pdfs)
  --run-tag                 : free-form tag stored in refresh_runs.params_json
  --db                      : catalog.sqlite path (default per-machine convention)

Writes:
  <target-dir>/<year>/<osti_id>.pdf
  pdf_fetch_log row per attempt
  file_instances row + papers.has_pdf=1 + canonical_* fields on success
  refresh_runs row (started_ts at launch, closed at end)

Retry policy: 503/429/timeout -> exp backoff up to 3 attempts.
"""
import sqlite3, os, sys, argparse, time, hashlib, urllib.request, urllib.error, ssl, random, json
from pathlib import Path
from datetime import datetime, timezone

UA = "Mozilla/5.0 (osti-corpus-backfill; rick.stevens.ai@gmail.com)"
TIMEOUT = 60
MAX_RETRY = 3


def introspect_schema(conn, tables):
    """Print actual column list per table. Fails loud at second 0 on schema drift."""
    cur = conn.cursor()
    print("=== SCHEMA INTROSPECTION ===")
    for t in tables:
        cols = cur.execute(f"SELECT name FROM pragma_table_info('{t}')").fetchall()
        if not cols:
            print(f"  [FATAL] table '{t}' does not exist in this DB", file=sys.stderr)
            sys.exit(2)
        print(f"  {t}: {', '.join(c[0] for c in cols)}")
    print()


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
    ap.add_argument("--db", required=True, help="Path to catalog.sqlite")
    ap.add_argument("--year-start", type=int, required=True)
    ap.add_argument("--year-end", type=int, required=True)
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--target-dir", required=True)
    ap.add_argument("--run-tag", default="backfill_v1")
    args = ap.parse_args()

    target_root = Path(args.target_dir)
    target_root.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db, timeout=600)
    conn.execute("PRAGMA journal_mode=WAL")

    # ====== FAIL LOUD ON SCHEMA DRIFT BEFORE FIRST INSERT ======
    introspect_schema(conn, ["papers", "file_instances", "pdf_fetch_log", "refresh_runs"])
    cur = conn.cursor()

    # Open refresh_runs row
    started_iso = datetime.now(timezone.utc).isoformat()
    cur.execute("""
        INSERT INTO refresh_runs (run_type, started_ts, params_json, records_added, records_updated, pdfs_added, errors)
        VALUES ('backfill_purl', ?, ?, 0, 0, 0, 0)
    """, (started_iso, json.dumps({
        "year_start": args.year_start, "year_end": args.year_end,
        "delay": args.delay, "limit": args.limit, "run_tag": args.run_tag,
        "target_dir": str(target_root),
    })))
    run_id = cur.lastrowid
    conn.commit()
    print(f"refresh_runs run_id={run_id}")

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

    counts = {}
    t0 = time.time()

    for i, (osti_id, year, lab) in enumerate(targets, 1):
        target_path = target_root / str(year) / f"{osti_id}.pdf"
        if target_path.exists():
            counts["already_present"] = counts.get("already_present", 0) + 1
            continue

        bucket, status, nbytes, sha, err = fetch(osti_id, target_path, args.delay)
        counts[bucket] = counts.get(bucket, 0) + 1

        ts = datetime.now(timezone.utc).isoformat()
        url = f"https://www.osti.gov/servlets/purl/{osti_id}"

        # SEPARATE TRY: log failure shouldn't block upsert below
        try:
            cur.execute("""
                INSERT INTO pdf_fetch_log (osti_id, run_id, ts, url, http_status, bytes, sha256, saved_path, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (osti_id, run_id, ts, url, status, nbytes, sha,
                  str(target_path) if bucket == "recovered_pdf" else "", err[:500]))
        except Exception as e:
            print(f"  log error {osti_id}: {e}", file=sys.stderr)

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

        if i % 50 == 0:
            conn.commit()
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
    """, (ended_iso, pdfs_added, errors, json.dumps(counts), run_id))
    conn.commit()
    conn.close()

    elapsed = (time.time() - t0) / 60
    print(f"\n=== Done in {elapsed:.1f}min ===")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:25s} {v:>6,}")


if __name__ == "__main__":
    main()
