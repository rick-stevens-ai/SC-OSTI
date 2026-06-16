"""
marker_worker_template.py — single-GPU Marker worker for ALCF batch jobs.

Reads pending rows from a shared SQLite manifest, runs Marker on each input PDF,
writes the .md atomically, updates the row to done/failed. Idempotent on restart.

Companion shell wrapper (marker_worker.sh) handles per-rank CUDA_VISIBLE_DEVICES
binding from PMI_LOCAL_RANK so MPI launches one worker per GPU.

Manifest schema (CREATE TABLE if not exists):
    items(
        id TEXT PRIMARY KEY,
        input_path TEXT NOT NULL,
        output_path TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',    -- pending|running|done|failed
        worker TEXT,
        started_at REAL,
        finished_at REAL,
        error TEXT
    )

Populate the manifest BEFORE submitting the PBS job:
    sqlite3 manifest.sqlite < schema.sql
    python populate_manifest.py /path/to/pdfs /path/to/md_out

Usage (per-worker, invoked by mpiexec):
    python marker_worker_template.py /path/to/manifest.sqlite
"""

import os
import sys
import sqlite3
import time
import traceback
from pathlib import Path

MANIFEST = sys.argv[1]
WORKER_ID = f"{os.uname().nodename}.{os.environ.get('PMI_RANK', '0')}.gpu{os.environ.get('CUDA_VISIBLE_DEVICES', '?')}"
POLL_INTERVAL = 2.0       # seconds between empty-queue polls
MAX_EMPTY_POLLS = 5       # exit after N empty polls in a row (queue drained)

def claim_next_item(conn):
    """Atomically claim the next pending row. Returns (id, input_path, output_path) or None."""
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    row = cur.execute(
        "SELECT id, input_path, output_path FROM items WHERE status='pending' LIMIT 1"
    ).fetchone()
    if not row:
        conn.commit()
        return None
    item_id, input_path, output_path = row
    cur.execute(
        "UPDATE items SET status='running', worker=?, started_at=? WHERE id=?",
        (WORKER_ID, time.time(), item_id),
    )
    conn.commit()
    return item_id, input_path, output_path

def mark_done(conn, item_id):
    conn.execute(
        "UPDATE items SET status='done', finished_at=? WHERE id=?",
        (time.time(), item_id),
    )
    conn.commit()

def mark_failed(conn, item_id, error):
    conn.execute(
        "UPDATE items SET status='failed', finished_at=?, error=? WHERE id=?",
        (time.time(), str(error)[:2000], item_id),
    )
    conn.commit()

def run_marker_on(input_pdf, output_md, models):
    """Run Marker. Writes output atomically (tmp + rename)."""
    from marker.converters.pdf import PdfConverter
    from marker.output import text_from_rendered

    converter = PdfConverter(artifact_dict=models)
    rendered = converter(input_pdf)
    text, _, _ = text_from_rendered(rendered)

    tmp = Path(output_md).with_suffix(".md.tmp")
    Path(output_md).parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, output_md)

def main():
    # Load models once per worker (expensive — ~30s cold)
    from marker.models import create_model_dict
    print(f"[{WORKER_ID}] loading marker models...", flush=True)
    models = create_model_dict()
    print(f"[{WORKER_ID}] models loaded", flush=True)

    # SQLite WAL mode for concurrent readers + atomic write claims
    conn = sqlite3.connect(MANIFEST, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    empty_polls = 0
    items_done = 0
    items_failed = 0

    while empty_polls < MAX_EMPTY_POLLS:
        try:
            claim = claim_next_item(conn)
        except sqlite3.OperationalError as e:
            # Lock contention on busy queue — wait briefly and retry
            print(f"[{WORKER_ID}] claim lock retry: {e}", flush=True)
            time.sleep(0.5)
            continue

        if claim is None:
            empty_polls += 1
            time.sleep(POLL_INTERVAL)
            continue
        empty_polls = 0

        item_id, input_path, output_path = claim
        t0 = time.time()
        try:
            run_marker_on(input_path, output_path, models)
            mark_done(conn, item_id)
            items_done += 1
            dt = time.time() - t0
            print(f"[{WORKER_ID}] done {item_id} in {dt:.1f}s  (total ok={items_done} fail={items_failed})", flush=True)
        except Exception as e:
            tb = traceback.format_exc()
            mark_failed(conn, item_id, f"{e}\n{tb[-1500:]}")
            items_failed += 1
            print(f"[{WORKER_ID}] FAILED {item_id}: {e}", flush=True)

    print(f"[{WORKER_ID}] queue drained, exiting. ok={items_done} fail={items_failed}", flush=True)
    conn.close()

if __name__ == "__main__":
    main()
