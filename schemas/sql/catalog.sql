-- catalog.sqlite schema dump
-- generated: 2026-06-16T13:19:29+00:00
-- source: /Volumes/SG-1-8TB/osti/catalog/catalog.sqlite
-- objects: 29

-- table: build_canonical_log
CREATE TABLE build_canonical_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_tag TEXT,
        ts TEXT,
        osti_id TEXT,
        source_path TEXT,
        target_path TEXT,
        outcome TEXT,   -- linked / already_linked / conflict_diff_inode / src_missing / no_year / skipped_in_layout
        note TEXT
    );

-- table: decisions
CREATE TABLE decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    osti_id TEXT,
    decision_type TEXT,
    chosen_instance_id INTEGER,
    rejected_instance_ids_json TEXT,
    rationale TEXT,
    method TEXT,
    confidence REAL,
    inputs_json TEXT
);

-- table: file_instances
CREATE TABLE file_instances (
    instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    osti_id TEXT,
    source TEXT,
    path TEXT UNIQUE,
    size INTEGER,
    sha256 TEXT,
    extracted_title TEXT,
    title_match_score REAL,
    first_seen_ts TEXT,
    last_verified_ts TEXT,
    is_canonical INTEGER DEFAULT 0,
    canonical_decision_id INTEGER
, canonical_path TEXT);

-- table: papers
CREATE TABLE papers (
    osti_id TEXT PRIMARY KEY,
    doi TEXT,
    title TEXT,
    publication_date TEXT,
    year INTEGER,
    product_type TEXT,
    journal_name TEXT,
    journal_volume TEXT,
    journal_issue TEXT,
    research_orgs_json TEXT,
    primary_lab TEXT,
    sponsor_orgs_json TEXT,
    authors_json TEXT,
    subjects_json TEXT,
    description TEXT,
    doe_contract_number TEXT,
    osti_links_json TEXT,
    catalog_first_seen_ts TEXT,
    catalog_last_seen_ts TEXT,
    metadata_source TEXT,
    has_pdf INTEGER DEFAULT 0,
    canonical_pdf_path TEXT,
    canonical_source TEXT,
    canonical_size INTEGER,
    canonical_sha256 TEXT,
    needs_pdf_fetch INTEGER DEFAULT 0,
    needs_ocr INTEGER DEFAULT 1,
    md_path TEXT,
    mmd_path TEXT,
    notes TEXT
, oa_status TEXT, oa_url TEXT, oa_license TEXT, oa_version TEXT, oa_evidence TEXT, oa_last_check TEXT, crossref_type TEXT, crossref_publisher TEXT, crossref_issn_json TEXT, crossref_subjects_json TEXT, crossref_last_check TEXT, osti_recheck_ts TEXT, osti_recheck_status TEXT, reconcile_state TEXT);

-- table: pdf_fetch_log
CREATE TABLE pdf_fetch_log (
    fetch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    osti_id TEXT,
    run_id INTEGER,
    ts TEXT,
    url TEXT,
    http_status INTEGER,
    bytes INTEGER,
    sha256 TEXT,
    saved_path TEXT,
    error TEXT
);

-- table: recovery_log
CREATE TABLE recovery_log (
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

-- table: recovery_queue
CREATE TABLE recovery_queue (
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

-- table: refresh_runs
CREATE TABLE refresh_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts TEXT,
    ended_ts TEXT,
    run_type TEXT,
    params_json TEXT,
    records_added INTEGER DEFAULT 0,
    records_updated INTEGER DEFAULT 0,
    pdfs_added INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    notes TEXT
);

-- index: idx_dec_osti
CREATE INDEX idx_dec_osti ON decisions(osti_id);

-- index: idx_dec_type
CREATE INDEX idx_dec_type ON decisions(decision_type);

-- index: idx_fi_canonical
CREATE INDEX idx_fi_canonical ON file_instances(is_canonical);

-- index: idx_fi_osti
CREATE INDEX idx_fi_osti ON file_instances(osti_id);

-- index: idx_fi_source
CREATE INDEX idx_fi_source ON file_instances(source);

-- index: idx_papers_crossref_check
CREATE INDEX idx_papers_crossref_check ON papers(crossref_last_check);

-- index: idx_papers_doi
CREATE INDEX idx_papers_doi ON papers(doi);

-- index: idx_papers_has_pdf
CREATE INDEX idx_papers_has_pdf ON papers(has_pdf);

-- index: idx_papers_lab
CREATE INDEX idx_papers_lab ON papers(primary_lab);

-- index: idx_papers_needs_pdf
CREATE INDEX idx_papers_needs_pdf ON papers(needs_pdf_fetch);

-- index: idx_papers_oa_check
CREATE INDEX idx_papers_oa_check ON papers(oa_last_check);

-- index: idx_papers_osti_recheck
CREATE INDEX idx_papers_osti_recheck ON papers(osti_recheck_ts);

-- index: idx_papers_year
CREATE INDEX idx_papers_year ON papers(year);

-- index: idx_pfl_osti
CREATE INDEX idx_pfl_osti ON pdf_fetch_log(osti_id);

-- index: idx_rl_osti
CREATE INDEX idx_rl_osti ON recovery_log(osti_id);

-- index: idx_rl_run
CREATE INDEX idx_rl_run ON recovery_log(run_id);

-- index: idx_rl_strategy
CREATE INDEX idx_rl_strategy ON recovery_log(strategy);

-- index: idx_rq_reason
CREATE INDEX idx_rq_reason ON recovery_queue(reason);

-- index: idx_rq_status
CREATE INDEX idx_rq_status ON recovery_queue(status);

-- index: ix_bcl_outcome
CREATE INDEX ix_bcl_outcome ON build_canonical_log(outcome);

-- index: ix_bcl_runtag
CREATE INDEX ix_bcl_runtag ON build_canonical_log(run_tag);

