#!/usr/bin/env python3
"""DOI-based PDF recovery worker.

Pulls from recovery_queue.status='pending'. For each paper, tries strategies
in order:
  1. unpaywall   - api.unpaywall.org by DOI -> best_oa_location.url_for_pdf
  2. semanticscholar - api.semanticscholar.org/graph/v1/paper/DOI:<doi>?fields=openAccessPdf
  3. crossref    - api.crossref.org/works/<doi> -> link[].URL where content-type=application/pdf

Every attempt logs a row to recovery_log (run_id, strategy, http_status, bytes, url, error).
On success, writes PDF, inserts file_instances, updates papers.has_pdf=1, marks queue.status='recovered'.
On full strategy exhaustion, marks queue.status='exhausted'.
Papers with no DOI in the catalog -> marks 'failed_no_doi'.

Can run as:
  - One-shot:   `python recovery_worker.py --batch 200`  (process up to 200 pending items, then exit)
  - Daemon:     `python recovery_worker.py --daemon --interval 600 --batch 500`  (loop, sleep between batches)

Bootstrap: on startup, if recovery_queue is empty, scans pdf_fetch_log for past failures
and seeds the queue from any (osti_id, bucket) where bucket != 'recovered_pdf' and not
already on disk.
"""
import sqlite3, os, sys, argparse, time, hashlib, urllib.request, urllib.error, ssl, json, random, signal
from pathlib import Path
from datetime import datetime, timezone

DB = "/Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite"
PDFS_ROOT = Path("/Volumes/Cherry6TB/osti_corpus/pdfs")
UNPAYWALL_EMAIL = "rick.stevens.ai@gmail.com"
UA = "Mozilla/5.0 (Kukla agent / osti-doi-recovery; rick.stevens.ai@gmail.com)"
TIMEOUT = 45

_should_stop = False
def _sigterm(_signo, _frame):
    global _should_stop
    _should_stop = True
    print("\n[SIGTERM] finishing current item, will exit clean")
signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT, _sigterm)


def db_exec(cur, sql, params=(), max_retry=10):
    """Execute with exponential backoff on 'database is locked'."""
    delay = 0.5
    for attempt in range(max_retry):
        try:
            return cur.execute(sql, params)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_retry - 1:
                time.sleep(delay + random.random() * 0.3)
                delay = min(delay * 1.8, 10.0)
                continue
            raise


def db_commit(conn, max_retry=10):
    delay = 0.5
    for attempt in range(max_retry):
        try:
            return conn.commit()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_retry - 1:
                time.sleep(delay + random.random() * 0.3)
                delay = min(delay * 1.8, 10.0)
                continue
            raise


def http_get_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, json.loads(r.read().decode("utf-8", errors="replace"))


def http_get_pdf(url, target_path, timeout=TIMEOUT):
    """Returns (status, bytes_written, sha256, error_msg). target_path on success."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            ctype = r.headers.get("Content-Type", "")
            head = r.read(8192)
            if not head.startswith(b"%PDF") and "pdf" not in ctype.lower():
                return (r.status, 0, "", f"not_pdf (ctype={ctype})")
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
                return (r.status, total, "", "size<=1024")
            tmp.rename(target_path)
            return (r.status, total, h.hexdigest(), "")
    except urllib.error.HTTPError as e:
        return (e.code, 0, "", str(e)[:300])
    except Exception as e:
        return (0, 0, "", str(e)[:300])


# --- Strategy: Unpaywall ---
def strategy_unpaywall(doi):
    """Returns (pdf_url, error_msg). pdf_url=None on no-result."""
    if not doi:
        return (None, "no_doi")
    url = f"https://api.unpaywall.org/v2/{doi}?email={UNPAYWALL_EMAIL}"
    try:
        status, data = http_get_json(url, timeout=30)
        loc = data.get("best_oa_location") or {}
        pdf_url = loc.get("url_for_pdf")
        if pdf_url:
            return (pdf_url, "")
        # fallback: any oa_locations[*].url_for_pdf
        for l in data.get("oa_locations") or []:
            if l.get("url_for_pdf"):
                return (l["url_for_pdf"], "")
        return (None, "no_oa_pdf")
    except urllib.error.HTTPError as e:
        return (None, f"unpaywall_http_{e.code}")
    except Exception as e:
        return (None, f"unpaywall_exc:{str(e)[:120]}")


# --- Strategy: Semantic Scholar ---
def strategy_s2(doi):
    if not doi:
        return (None, "no_doi")
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=openAccessPdf"
    try:
        status, data = http_get_json(url, timeout=30)
        oa = data.get("openAccessPdf") or {}
        pdf_url = oa.get("url")
        if pdf_url:
            return (pdf_url, "")
        return (None, "no_openAccessPdf")
    except urllib.error.HTTPError as e:
        # 429 rate-limit common on anon tier
        return (None, f"s2_http_{e.code}")
    except Exception as e:
        return (None, f"s2_exc:{str(e)[:120]}")


# --- Strategy: Crossref ---
def strategy_crossref(doi):
    if not doi:
        return (None, "no_doi")
    url = f"https://api.crossref.org/works/{doi}"
    try:
        status, data = http_get_json(url, timeout=30)
        msg = data.get("message", {})
        links = msg.get("link") or []
        # Prefer application/pdf and intended-application='text-mining' (TDM)
        scored = []
        for l in links:
            ct = (l.get("content-type") or "").lower()
            ia = (l.get("intended-application") or "").lower()
            score = 0
            if "pdf" in ct: score += 10
            if ia == "text-mining": score += 5
            if ia == "similarity-checking": score += 3
            if score > 0:
                scored.append((score, l.get("URL")))
        if scored:
            scored.sort(reverse=True)
            return (scored[0][1], "")
        return (None, "no_pdf_links")
    except urllib.error.HTTPError as e:
        return (None, f"crossref_http_{e.code}")
    except Exception as e:
        return (None, f"crossref_exc:{str(e)[:120]}")


STRATEGIES = [
    ("unpaywall", strategy_unpaywall),
    ("s2", strategy_s2),
    ("crossref", strategy_crossref),
]


def log_attempt(cur, osti_id, run_id, strategy, doi, url, http_status, nbytes, sha, saved, error, duration_ms):
    ts = datetime.now(timezone.utc).isoformat()
    try:
        db_exec(cur, """
            INSERT INTO recovery_log
                (osti_id, run_id, ts, strategy, doi, source_url, http_status, bytes, sha256, saved_path, error, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (osti_id, run_id, ts, strategy, doi, url, http_status, nbytes, sha, saved, error[:500], duration_ms))
    except Exception as e:
        print(f"  [log error {osti_id}/{strategy}] {e}", file=sys.stderr)


def process_one(conn, cur, run_id, osti_id, reason, doi, year, inter_strategy_delay):
    """Returns final status string for queue: recovered / exhausted / failed_no_doi."""
    if not doi:
        # Skip strategy loop — nothing to query without DOI
        log_attempt(cur, osti_id, run_id, "skip_no_doi", None, "", 0, 0, "", "", "no_doi_in_catalog", 0)
        return ("failed_no_doi", "skip_no_doi", "no_doi")

    target_path = PDFS_ROOT / str(year) / f"{osti_id}.pdf"
    if target_path.exists():
        return ("recovered", "already_present", "")

    strategies_tried = []
    last_strategy = ""
    last_err = ""

    for name, fn in STRATEGIES:
        if _should_stop:
            return ("pending", last_strategy, "interrupted")
        strategies_tried.append(name)
        last_strategy = name
        t0 = time.time()
        pdf_url, err = fn(doi)
        if not pdf_url:
            log_attempt(cur, osti_id, run_id, name, doi, "", 0, 0, "", "", err, int((time.time()-t0)*1000))
            last_err = err
            time.sleep(inter_strategy_delay)
            continue
        # Try to fetch
        t1 = time.time()
        status, nbytes, sha, ferr = http_get_pdf(pdf_url, target_path)
        duration_ms = int((time.time()-t1)*1000)
        if nbytes > 1024 and sha:
            log_attempt(cur, osti_id, run_id, name, doi, pdf_url, status, nbytes, sha, str(target_path), "", duration_ms)
            # Insert file_instances + update papers
            ts = datetime.now(timezone.utc).isoformat()
            try:
                db_exec(cur, """
                    INSERT OR IGNORE INTO file_instances
                        (osti_id, source, path, size, sha256, is_canonical, first_seen_ts, last_verified_ts)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """, (osti_id, f"recovery_{name}", str(target_path), nbytes, sha, ts, ts))
                db_exec(cur, """
                    UPDATE papers SET has_pdf=1, canonical_pdf_path=?, canonical_source=?,
                                      canonical_size=?, canonical_sha256=?
                    WHERE osti_id=?
                """, (str(target_path), f"recovery_{name}", nbytes, sha, osti_id))
            except Exception as e:
                print(f"  upsert error {osti_id}: {e}", file=sys.stderr)
            return ("recovered", name, "")
        else:
            log_attempt(cur, osti_id, run_id, name, doi, pdf_url, status, nbytes, "", "", ferr, duration_ms)
            last_err = f"{name}_fetch:{ferr}"
            time.sleep(inter_strategy_delay)

    return ("exhausted", last_strategy, last_err)


def bootstrap_queue(conn, cur):
    """If recovery_queue is empty, seed it from pdf_fetch_log failures."""
    n = cur.execute("SELECT COUNT(*) FROM recovery_queue").fetchone()[0]
    if n > 0:
        return n
    print("recovery_queue empty — bootstrapping from pdf_fetch_log failures...")
    db_exec(cur, """
        INSERT OR IGNORE INTO recovery_queue (osti_id, reason, enqueued_ts, status)
        SELECT osti_id,
               CASE
                 WHEN http_status = 404 THEN 'http_404'
                 WHEN http_status = 403 THEN 'http_403'
                 WHEN http_status = 503 THEN 'http_503'
                 WHEN http_status = 200 AND bytes <= 1024 THEN 'empty'
                 WHEN http_status = 200 AND (saved_path = '' OR saved_path IS NULL) THEN 'wrong_type_or_redirect'
                 WHEN http_status = 0 THEN 'timeout_or_exception'
                 ELSE 'other'
               END AS reason,
               MIN(ts) AS enqueued_ts,
               'pending'
        FROM pdf_fetch_log
        WHERE osti_id NOT IN (SELECT osti_id FROM papers WHERE has_pdf=1)
        GROUP BY osti_id
    """)
    db_commit(conn)
    n2 = cur.execute("SELECT COUNT(*) FROM recovery_queue").fetchone()[0]
    print(f"  seeded {n2} items")
    return n2


def run_batch(conn, cur, run_id, batch_size, inter_paper_delay, inter_strategy_delay):
    """Process up to batch_size pending items. Returns (processed, recovered)."""
    rows = cur.execute("""
        SELECT q.osti_id, q.reason, p.doi, p.year
        FROM recovery_queue q
        JOIN papers p ON p.osti_id = q.osti_id
        WHERE q.status = 'pending'
        ORDER BY q.enqueued_ts
        LIMIT ?
    """, (batch_size,)).fetchall()
    if not rows:
        return (0, 0)

    print(f"Processing batch of {len(rows)} pending items...")
    processed = recovered = 0
    counts = {}
    t0 = time.time()
    for i, (osti_id, reason, doi, year) in enumerate(rows, 1):
        if _should_stop:
            break
        # Claim it
        db_exec(cur, """
            UPDATE recovery_queue
            SET status='in_progress', last_attempt_ts=?, attempts = attempts + 1
            WHERE osti_id=?
        """, (datetime.now(timezone.utc).isoformat(), osti_id))
        db_commit(conn)

        final_status, last_strategy, last_err = process_one(conn, cur, run_id, osti_id, reason, doi, year, inter_strategy_delay)
        counts[final_status] = counts.get(final_status, 0) + 1
        processed += 1
        if final_status == "recovered":
            recovered += 1

        # Update queue row
        resolved_ts = datetime.now(timezone.utc).isoformat() if final_status == "recovered" else None
        db_exec(cur, """
            UPDATE recovery_queue
            SET status=?, last_strategy=?, last_error=?, resolved_via=?, resolved_ts=?
            WHERE osti_id=?
        """, (final_status, last_strategy, last_err[:500] if last_err else "",
              last_strategy if final_status == "recovered" else None,
              resolved_ts, osti_id))
        if i % 25 == 0:
            db_commit(conn)
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed > 0 else 0
            eta = (len(rows) - i) / rate / 60 if rate > 0 else 0
            print(f"  {i}/{len(rows)} rate={rate:.2f}/s eta={eta:.0f}min counts={counts}", flush=True)

        time.sleep(inter_paper_delay)

    db_commit(conn)
    return (processed, recovered)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=200, help="Items per batch")
    ap.add_argument("--daemon", action="store_true", help="Loop forever between batches")
    ap.add_argument("--interval", type=int, default=300, help="Daemon sleep between batches when no pending items (s)")
    ap.add_argument("--inter-paper-delay", type=float, default=0.5)
    ap.add_argument("--inter-strategy-delay", type=float, default=0.3)
    ap.add_argument("--max-batches", type=int, default=None, help="Daemon mode: stop after N batches")
    args = ap.parse_args()

    conn = sqlite3.connect(DB, timeout=600)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    cur = conn.cursor()

    bootstrap_queue(conn, cur)

    # Open refresh_runs row
    started_iso = datetime.now(timezone.utc).isoformat()
    db_exec(cur, """
        INSERT INTO refresh_runs (run_type, started_ts, params_json, records_added, records_updated, pdfs_added, errors)
        VALUES ('recovery_doi', ?, ?, 0, 0, 0, 0)
    """, (started_iso, json.dumps({
        "batch": args.batch, "daemon": args.daemon, "interval": args.interval,
        "inter_paper_delay": args.inter_paper_delay, "inter_strategy_delay": args.inter_strategy_delay,
    })))
    run_id = cur.lastrowid
    db_commit(conn)
    print(f"refresh_runs run_id={run_id}, daemon={args.daemon}")

    total_processed = total_recovered = 0
    batches = 0
    try:
        while True:
            if _should_stop:
                print("[stop] exiting outer loop")
                break
            processed, recovered = run_batch(conn, cur, run_id, args.batch,
                                              args.inter_paper_delay, args.inter_strategy_delay)
            total_processed += processed
            total_recovered += recovered
            batches += 1
            print(f"\n[batch {batches}] processed={processed} recovered={recovered}"
                  f" total_processed={total_processed} total_recovered={total_recovered}")

            if not args.daemon:
                break
            if args.max_batches and batches >= args.max_batches:
                print(f"[max-batches reached] stopping")
                break
            if processed == 0:
                print(f"[no pending] sleeping {args.interval}s")
                for _ in range(args.interval):
                    if _should_stop: break
                    time.sleep(1)
    finally:
        ended_iso = datetime.now(timezone.utc).isoformat()
        db_exec(cur, """
            UPDATE refresh_runs
            SET ended_ts=?, pdfs_added=?, notes=?
            WHERE run_id=?
        """, (ended_iso, total_recovered, json.dumps({
            "total_processed": total_processed,
            "total_recovered": total_recovered,
            "batches": batches,
        }), run_id))
        db_commit(conn)
        conn.close()
        print(f"\n=== Exiting: processed={total_processed} recovered={total_recovered} batches={batches} ===")


if __name__ == "__main__":
    main()
