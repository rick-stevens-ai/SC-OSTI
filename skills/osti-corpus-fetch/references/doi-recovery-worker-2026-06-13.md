# DOI recovery worker: concurrent-fetcher architecture (2026-06-13)

Companion to `scripts/recovery_worker.py` and `scripts/bulk_fetch_purl.py`. Captures the design decisions and pitfalls from building the 2-process recovery pipeline: bulk PURL fetcher feeds a queue, DOI worker drains it, both write to the same catalog DB concurrently.

## The architecture

```
┌─────────────────────────────┐         ┌─────────────────────────────┐
│ bulk_fetch_purl.py          │         │ recovery_worker.py          │
│ (PURL fetcher, year-ranged) │         │ (DOI cascade, queue-driven) │
│                             │         │                             │
│ for each paper missing PDF: │         │ while queue has pending:    │
│   try PURL                  │         │   for each item:            │
│   on success:               │         │     try unpaywall           │
│     write PDF               │         │     try s2                  │
│     papers.has_pdf=1        │         │     try crossref            │
│     +file_instances row     │         │     on first hit:           │
│   on failure:               │         │       write PDF             │
│     +pdf_fetch_log row      │         │       papers.has_pdf=1      │
│     +recovery_queue row     │         │       +file_instances row   │
│       (pending)             │         │     log each strategy       │
└──────────┬──────────────────┘         │       attempt to            │
           │                            │       recovery_log          │
           │                            │     mark queue row          │
           │                            │       recovered/exhausted/  │
           │                            │       failed_no_doi         │
           ▼                            └──────────┬──────────────────┘
        ┌──────────────────────────────────────────┴────┐
        │ catalog.sqlite                                │
        │ ───────────────                               │
        │ papers (has_pdf, canonical_*)                 │
        │ file_instances (per PDF on disk)              │
        │ pdf_fetch_log (every PURL attempt)            │
        │ recovery_queue (DOI work items + status)      │
        │ recovery_log (every DOI strategy attempt)     │
        │ refresh_runs (one row per process invocation) │
        └───────────────────────────────────────────────┘
```

Two long-running processes, one shared SQLite catalog. The fetcher produces queue items; the worker drains them. Every per-paper attempt — from any process, any strategy — is persisted with full provenance (run_id, ts, http_status, bytes, sha256, error).

## Why split into two processes instead of one pipeline

1. **Different rate envelopes.** PURL fetcher: OSTI sustains ~1 req/s sequential. DOI worker: Unpaywall+S2+Crossref each have separate rate limits and longer per-paper wall (3 strategies × ~1s + fetch ~2s = ~5s per paper). Coupling them serializes the slower path and wastes the fetcher's headroom.
2. **Independent restart.** If one crashes (schema drift, network blip, OOM), the other keeps making progress. The queue table is the handoff buffer; either side can be killed and restarted without losing state.
3. **Different parallelism plans.** Fetcher is single-process today but could shard by year range. Worker could shard by strategy or split into per-strategy workers (one for Unpaywall, one for S2). Decoupled architectures support that; pipelined ones don't.
4. **Bootstrap-from-log resilience.** If the fetcher started BEFORE the enqueue patch was in place (real case 2026-06-13 — already-running fetcher had the old code), the worker's bootstrap path re-derives the queue from `pdf_fetch_log`. The queue is a cache, not the source of truth.

## Schema additions (recovery_queue + recovery_log)

Run `_state/schema_recovery.sql` first to add these. Both tables are idempotent (`CREATE IF NOT EXISTS`).

### recovery_queue (work items, one row per paper)

| column | type | role |
|---|---|---|
| `osti_id` | TEXT PK | one entry per paper |
| `reason` | TEXT | why it landed here: `http_404` / `http_403` / `http_503` / `empty` / `wrong_type_or_redirect` / `timeout_or_exception` |
| `enqueued_ts` | TEXT ISO | when first added |
| `enqueued_run_id` | INTEGER | refresh_runs row that enqueued it |
| `status` | TEXT | `pending` → `in_progress` → `recovered` / `exhausted` / `failed_no_doi` |
| `attempts` | INTEGER | bumped each time the worker claims it |
| `strategies_tried` | TEXT JSON | array of strategy names tried so far |
| `last_attempt_ts` | TEXT | when worker last touched it |
| `last_strategy` | TEXT | most recent strategy attempted |
| `last_error` | TEXT | most recent failure reason |
| `resolved_via` | TEXT | on success: strategy that worked |
| `resolved_ts` | TEXT | on success: when |
| `notes` | TEXT | free-form |

Index on `status` (frequent SELECT) and `reason` (for failure-mode rollups).

### recovery_log (one row per strategy attempt, never overwritten)

| column | type | role |
|---|---|---|
| `log_id` | INTEGER PK AUTOINC | |
| `osti_id` | TEXT | |
| `run_id` | INTEGER | refresh_runs FK |
| `ts` | TEXT ISO | |
| `strategy` | TEXT | `unpaywall` / `s2` / `crossref` / `skip_no_doi` |
| `doi` | TEXT | |
| `source_url` | TEXT | URL we tried to fetch (post-API-lookup) |
| `http_status` | INTEGER | |
| `bytes` | INTEGER | |
| `sha256` | TEXT | |
| `saved_path` | TEXT | populated only on success |
| `error` | TEXT | short error message, truncated to 500 chars |
| `duration_ms` | INTEGER | end-to-end strategy wall time |

Indexes on `osti_id`, `strategy`, `run_id` — supports "show all attempts on paper X," "what's the S2 hit rate," "post-mortem of run N."

## Strategy cascade rationale

Order is `unpaywall → s2 → crossref`. Picked for these reasons:

| strategy | when it wins | when it fails |
|---|---|---|
| Unpaywall | OA papers with deposited repository copies (arXiv, PMC, institutional repos) | publisher OA flag without scraping rights; pre-DOI-era papers |
| S2 | Their `openAccessPdf` field uses a different aggregation than Unpaywall (catches some Unpaywall misses) | rate-limited hard on anon tier (use API key, see osti-corpus-fetch SKILL.md pitfall) |
| Crossref | TDM-licensed links (`intended-application=text-mining`) for publisher PDFs | most publisher entries have no `link[]` array at all |

**Skip S2 in the worker for now** unless API key is wired — anon tier 429s consistently and burns time. The worker as-written calls S2 anonymously and treats 429 as a failure; rewire to use the keychain key when productionizing.

## The concurrent-writer lock contention problem (load-bearing)

The defining pitfall of this design. Both processes want to write to the same `catalog.sqlite`. WAL mode allows readers + one writer, but writers serialize on the database write lock. With two writers running concurrently:

- Fetcher commits every 50 rows × 0.8s = ~40s lock window per commit cycle
- Worker tries to claim a queue row (UPDATE), commit, then try strategies (~5s of HTTP), then update again, commit
- Worker's commit lands inside the fetcher's lock window roughly 1 in 8 times
- Without retry, worker raises `sqlite3.OperationalError: database is locked` and dies

`sqlite3.connect(..., timeout=600)` and `PRAGMA busy_timeout=60000` were supposed to handle this transparently, but in practice **they only protect ONE busy-wait per connection state machine** — under sustained contention with many small commits, the second write in a quick burst gets the immediate error rather than waiting.

**The fix that works**: explicit retry-with-backoff shim wrapping every write. See `db_exec()` and `db_commit()` in `scripts/recovery_worker.py`:

```python
def db_exec(cur, sql, params=(), max_retry=10):
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
```

10 attempts, 0.5s starting delay, 1.8× multiplier, 10s ceiling — total worst-case ~30s before giving up (longer than the fetcher's typical lock window).

**Wrap EVERYTHING.** I missed three writes on the first pass and crashed three times before getting them all:
1. The bootstrap INSERT (`INSERT OR IGNORE INTO recovery_queue SELECT ... FROM pdf_fetch_log`)
2. The initial `refresh_runs` INSERT in `main()`
3. The close-out `refresh_runs` UPDATE in `finally:`

The audit pattern after writing the script: `grep -nE "cur\.execute|conn\.commit"` and verify every line either is a SELECT (safe) or goes through the shim. SELECTs don't need the shim — WAL gives them snapshot-isolation without acquiring the write lock.

## Bootstrap-from-log pattern

If `recovery_queue` is empty on worker startup, seed it from `pdf_fetch_log` failures:

```sql
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
GROUP BY osti_id;
```

This handles the "fetcher started before enqueue patch" case AND the "queue table was just created" case AND the "queue got dropped/migrated" case. The `INSERT OR IGNORE` makes it safe to rerun — already-queued items are unaffected.

The fetcher SHOULD also enqueue inline (see the patched bulk_fetch_purl.py — the `else:` branch on `if bucket == "recovered_pdf":`) so the queue stays current without depending on bootstrap. Bootstrap is the backstop.

## Daemon vs one-shot mode

The worker supports both:

- **One-shot** (`python recovery_worker.py --batch 200`): processes up to 200 pending items, then exits. Useful for cron or for "drain whatever's in the queue right now."
- **Daemon** (`python recovery_worker.py --daemon --batch 500 --interval 600`): processes a batch, then loops. When the queue is empty, sleeps `interval` seconds and re-checks. Runs forever (until SIGTERM). Useful for sustained drain alongside a fetcher that's continuously producing.

For a multi-hour fetcher run, launch the worker as daemon with a polite delay (`--inter-paper-delay 0.4 --inter-strategy-delay 0.25`) to keep API-side rate budgets in check.

## SIGTERM handling

Both processes install a SIGTERM/SIGINT handler that sets a global `_should_stop` flag. Hot loops check the flag between iterations and exit cleanly — committing the in-progress row's state (so it doesn't get stuck in `in_progress` forever) and closing the refresh_runs row.

If the worker is killed harshly (`kill -9`, OOM), some items end up stuck in `status='in_progress'`. Recovery: `UPDATE recovery_queue SET status='pending' WHERE status='in_progress';` — they'll be re-claimed on next batch.

## Capacity planning

From the 2026-06-13 session, against the 2000-2005 backfill pool (24,945 papers):

- **PURL fetcher rate**: ~0.4-0.7 papers/s under contention with cels rsync running. ETA 12-16h for full pool.
- **PURL recovery rate**: ~28% (varies 24-32% by smoke).
- **Failure distribution**: ~67% http_404, ~25% redirect_off (off-site landing pages), ~5% transient (503/timeout/exception). Queue will fill with ~18K items from the 2000-2005 pool.
- **DOI worker rate** (anon S2, sequential strategies): ~0.1-0.2 papers/s. ~5 strategies × 1s API + 2s fetch = 7s/paper worst case.
- **Pre-2006 DOI coverage**: very low — many papers from 2000-2005 era don't have DOIs in the catalog at all (papers.doi IS NULL). Those resolve to `failed_no_doi` in <0.1s.

For a real run, **wire the S2 API key** (see osti-corpus-fetch SKILL.md pitfall). Anon tier is too slow at scale.

## Reusable beyond OSTI

The pattern — two concurrent writers, one shared catalog, retry-shim on every write, queue table as handoff buffer, separate log table for full per-strategy provenance — generalizes to:

- **arXiv harvest + paper-PDF backfill**: harvester populates metadata, backfill worker fetches PDFs by strategy
- **HF Hub mirror**: model listing producer + per-model file fetcher
- **Scraper + reprocessor**: page scraper writes raw HTML, reprocessor extracts structured data, both update the same row
- **Classifier + corrector**: judgment pipeline writes initial verdict, human-in-the-loop corrector overrides

The load-bearing parts are: (1) the retry shim, (2) the queue table with status state machine, (3) the per-attempt log table, (4) the bootstrap-from-log pattern for queue recovery.

## Pitfalls (recap, fast-reference)

1. Wrap **every** write+commit in the retry shim. Not just hot path — bootstrap, init, close-out too. Missing even one means a crash window.
2. **`grep -nE "cur\.execute|conn\.commit"`** the script before launch. Every match should either be a SELECT or shimmed.
3. **Schema introspect at script init** — see `osti-corpus-fetch` SKILL.md pitfall on never-write-from-memory.
4. **Separate try blocks** for the log INSERT vs the canonical-state UPSERT. One failure should not poison the other.
5. **Unstick `in_progress` items** after any harsh kill: `UPDATE recovery_queue SET status='pending' WHERE status='in_progress';`
6. **S2 anon tier 429s hard**. Wire the API key (see osti-corpus-fetch SKILL.md pitfall on S2 API key).
7. **Pre-DOI-era papers (~2000-2005) resolve to `failed_no_doi` fast** — won't budge the recovery curve. Different strategy needed (title+author search, Google Scholar) for that bucket.
