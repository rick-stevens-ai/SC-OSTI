-- DOI recovery queue + log schema additions
-- Idempotent: CREATE IF NOT EXISTS

CREATE TABLE IF NOT EXISTS recovery_queue (
    osti_id          TEXT PRIMARY KEY,
    reason           TEXT,           -- 404, wrong_type, redirect_off, http_403, timeout, exception
    enqueued_ts      TEXT,
    enqueued_run_id  INTEGER,        -- which refresh_runs row first put this on the queue
    status           TEXT DEFAULT 'pending',  -- pending, in_progress, recovered, exhausted, failed_no_doi
    attempts         INTEGER DEFAULT 0,
    strategies_tried TEXT,           -- json array: ["unpaywall","s2","crossref"]
    last_attempt_ts  TEXT,
    last_strategy    TEXT,
    last_error       TEXT,
    resolved_via     TEXT,           -- on success: strategy that worked
    resolved_ts      TEXT,
    notes            TEXT
);
CREATE INDEX IF NOT EXISTS idx_rq_status ON recovery_queue(status);
CREATE INDEX IF NOT EXISTS idx_rq_reason ON recovery_queue(reason);

CREATE TABLE IF NOT EXISTS recovery_log (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    osti_id          TEXT,
    run_id           INTEGER,        -- FK refresh_runs.run_id
    ts               TEXT,
    strategy         TEXT,           -- unpaywall_api, s2_api, crossref_resolve, direct_publisher
    doi              TEXT,
    source_url       TEXT,           -- the URL we actually fetched
    http_status      INTEGER,
    bytes            INTEGER,
    sha256           TEXT,
    saved_path       TEXT,
    error            TEXT,
    duration_ms      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rl_osti ON recovery_log(osti_id);
CREATE INDEX IF NOT EXISTS idx_rl_strategy ON recovery_log(strategy);
CREATE INDEX IF NOT EXISTS idx_rl_run ON recovery_log(run_id);
