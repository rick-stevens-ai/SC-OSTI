# Iterative Fetch State Machine — Phase E pattern

Captured 2026-06-10 from the Phase E comprehensive plan
(`~/code/osti-replication-candidates/PHASE_E_PLAN.md`).
This is the **multi-strategy recovery pipeline pattern** for closing a residual
PDF gap of N×10K papers after the headline OSTI-PURL + Unpaywall passes have
exhausted their yield.

Use when the gap is dominated by `skip_no_url` (Unpaywall says no OA) and
`http_403` (publisher refused) buckets, and a single fallback isn't enough.

## When to reach for this pattern

- Headline coverage from `osti_purl + unpaywall` plateaus well below the target.
- Residual is large (10K+ papers) and has DOIs but Unpaywall says no OA.
- You have multiple free APIs available (Crossref, S2, arXiv) and per-publisher
  recipes (APS, ACS, Wiley) but no single one will close the gap.
- The work must be resumable, auditable per attempt, and able to grow new
  strategies later without re-running prior ones.

If the gap is < 1K papers or a single fallback API would suffice, use the
existing `unpaywall_overnight.py` shape instead — don't over-build.

## Architecture in one paragraph

A SQLite state DB (WAL mode, on local fast SSD — not on a network/USB volume)
is the single source of truth. A driver process pulls `target_state='pending'`
papers FIFO and walks each through an ordered strategy chain
(`osti_purl → unpaywall → crossref → s2 → arxiv → publisher_html`). First
valid PDF wins; every attempt logged with status + bytes + elapsed. Workers
are per-strategy thread pools with per-service rate limits. Output goes to
local staging then mirrors to Cherry6TB nightly. Periodic reporter reads the
state DB and produces per-strategy/per-lab/per-year hit tables and a
residual-cost analysis.

## State DB schema (canonical)

```sql
CREATE TABLE papers (
    osti_id          TEXT    PRIMARY KEY,
    doi              TEXT,
    doi_prefix       TEXT,
    title            TEXT,
    lab              TEXT,
    year             INTEGER,
    product_type     TEXT,
    target_state     TEXT    DEFAULT 'pending'
                             CHECK(target_state IN ('pending','in_flight','done','exhausted','skip')),
    final_status     TEXT,        -- ok|no_doi|no_oa|policy_walled|gone|exhausted
    winning_strategy TEXT,
    pdf_path         TEXT,
    pdf_bytes        INTEGER,
    pdf_pages        INTEGER,
    pdf_chars        INTEGER,
    last_attempt_ts  REAL,
    n_attempts       INTEGER DEFAULT 0,
    created_ts       REAL    DEFAULT (julianday('now'))
);

CREATE TABLE attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    osti_id      TEXT,
    strategy     TEXT,
    status       TEXT,
    http_code    INTEGER,
    bytes        INTEGER,
    pdf_pages    INTEGER,
    elapsed_s    REAL,
    url          TEXT,
    error        TEXT,
    ts           REAL,
    FOREIGN KEY (osti_id) REFERENCES papers(osti_id)
);

CREATE TABLE strategy_health (
    strategy        TEXT PRIMARY KEY,
    enabled         INTEGER DEFAULT 1,
    last_disabled_reason TEXT,
    recent_failures INTEGER DEFAULT 0,
    consecutive_5xx INTEGER DEFAULT 0,
    updated_ts      REAL
);

CREATE INDEX idx_papers_state    ON papers(target_state);
CREATE INDEX idx_papers_lab_yr   ON papers(lab, year);
CREATE INDEX idx_attempts_osti   ON attempts(osti_id);
CREATE INDEX idx_attempts_strat  ON attempts(strategy, status);

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
```

Atomic claim pattern (so multiple workers don't double-fetch):

```sql
BEGIN IMMEDIATE;
UPDATE papers SET target_state='in_flight'
 WHERE osti_id IN (SELECT osti_id FROM papers
                    WHERE target_state='pending'
                    LIMIT :batch_size)
 RETURNING *;
COMMIT;
```

## Strategy chain (priority-ordered, calibrated 2026-06-10)

Estimated yield per strategy on residual, calibrated from Phase 3 v2 (3,874
paper sample) + Phase C live data (21K probed @ 11.4% Unpaywall hit):

| # | Strategy | Precondition | Yield on residual | Calibrated from |
|---|---|---|---|---|
| 1 | `osti_purl` | always | 28% | Phase 3 v2 fresh re-probe from DOE IP |
| 2 | `unpaywall` | has DOI, not OSTI-internal prefix | 11.4% | Phase C live |
| 3 | `crossref` | has DOI | 2-3% | Crossref OA links (distinct from Unpaywall) |
| 4 | `s2` | has DOI or title | 4-6% | S2 `openAccessPdf.url`, API-keyed |
| 5 | `arxiv` | physics/CS/astro labs first | 6-10% | DOE physics disproportionately preprints |
| 6 | `publisher_html` | landing-page URL, known publisher | 2-4% | APS/ACS/Wiley OA per-publisher recipes |
| 7 | `google_scholar` | last-resort, deferred to E.3 | <1% | Captcha-walled |

**Cumulative ceiling on Phase E.1+E.2+E.3:** ~50K new PDFs from a ~125K gap,
lifting coverage 46.5% → ~67% (matches Phase 3 v2 "any-source 68.7%
recoverable" estimate).

### Live failure-bucket distribution (Phase C, 21K probed)

This is what told us crossref/s2/arxiv are the right fallbacks to build —
39.6% of papers had a DOI but no Unpaywall OA URL.

```
skip_no_url     8,266  (39.6%)  no Unpaywall OA URL  → crossref/s2/arxiv
http_403        4,532  (21.7%)  publisher refused    → publisher_html or VPN
ok              2,368  (11.3%)  Unpaywall delivered
not_pdf_html    3,659  (17.5%)  landing page         → publisher_html scrape
too_small       1,202   (5.8%)  gateway-block stubs  → flag as policy_walled
http_4xx other    228   (1.1%)  404/405/410          → mark gone
http_5xx           82   (0.4%)  transient            → retry
```

## State DB location decision

**Always put the state DB on local fast SSD where the driver runs**, not on a
network volume or USB-mounted external. Reasons:

- SQLite WAL relies on filesystem-level locking that doesn't always work
  cleanly over NFS/CIFS/HFS+ via USB.
- Concurrent writes from N strategy workers + the driver = lock contention
  hot loop on slow media, dropping throughput to single-digit ops/sec.
- USB-mounted volumes can drop the mount mid-run, corrupting an open WAL.

Pattern: **state DB on `cels-rbdgx2:/rbstor/stevens/<project>_state.db`**
(local NVMe), **nightly snapshot via `.backup` command to Cherry6TB** for
backup + reporter access:

```bash
ssh cels-rbdgx2 'sqlite3 /rbstor/stevens/osti_recovery_state.db \
  ".backup /tmp/state.snapshot.db"'
scp cels-rbdgx2:/tmp/state.snapshot.db \
  /Volumes/Cherry6TB/snapshots/state_$(date +%Y%m%d).db
```

Cron at 04:45 daily, after the recovered-PDF mirror at 04:30.

The first draft of `PHASE_E_DESIGN.md` put the state DB on Cherry6TB
directly. Corrected in the comprehensive plan — see "Open Issues" §1.

## Strategy module shape

Each strategy is a class implementing the same interface:

```python
class AbstractStrategy:
    name: str
    req_per_sec: float

    def applies(self, paper: dict) -> bool:
        """Cheap precondition check (DOI present, prefix not blacklisted, etc.)."""

    def fetch(self, paper: dict, out_dir: str) -> dict:
        """Returns {status, pdf_path|None, bytes, elapsed_s, url, error, http_code}.

        status enum: ok | skip_no_url | not_oa | http_403 | http_404 | http_5xx |
                     not_pdf_html | too_small | timeout | tls | dns_err
        """
```

Concrete strategies live in `strategies/<name>.py`. Driver iterates
`STRATEGY_CHAIN` in order, calls `applies()` first (skip if False without
logging), then `fetch()` and logs every attempt.

## User-Agent discipline

Honest identification, no browser masquerading:

```
User-Agent: Argonne-OSTI-archival-Kukla/1.0 (rick.stevens@uchicago.edu)
```

Reasons:
- Crossref/Unpaywall/S2 all run a "polite pool" with more generous rate limits
  for identified clients (`mailto=` header or User-Agent with email).
- Truthful UA is a soft license-to-operate; the moment you spoof a browser
  you've opted into the adversarial rate-limit regime.
- If something does break, the operator can find Rick's email and ask us to
  stop, instead of just IP-banning.

## Per-strategy rate limits (verified safe)

- **OSTI PURL:** 2 req/s (no formal published limit; this is the polite floor)
- **Unpaywall:** 8 req/s with `email=` param
- **Crossref:** 50 req/s with `mailto=` header (polite pool)
- **Semantic Scholar:** 1 req/s anonymous; 7-10 req/s with API key
- **arXiv:** 1 req/s (their published polite limit)
- **Publisher HTML scrapes:** 1 req/s per netloc (don't pound any one publisher)

## Strategy health monitor

Driver background task every 60s checks `attempts` table:

- If a strategy has `recent_failures > 50` in last 5 minutes → disable
- If `consecutive_5xx > 10` → disable, mark in `strategy_health` table
- Re-enable after 10-minute cooldown
- Log to `phase_e/health.log` so operator sees disables

Prevents one flaky service from poisoning the whole driver and burning
through the residual queue with all-failure attempts.

## What goes in `final_status`

After all applicable strategies have failed, the paper is marked
`target_state='exhausted'` and assigned a `final_status` enum value
based on the most common failure across attempts:

| final_status | Meaning | Next action |
|---|---|---|
| `ok` | Got a valid PDF | Already done |
| `no_doi` | No DOI, no title-match anywhere | Genuinely unrecoverable |
| `no_oa` | DOI exists, no OA copy anywhere | Email publisher / accept loss |
| `policy_walled` | OSTI biblio shows publisher link but PURL = 403 | Email `comments@osti.gov` for these |
| `gone` | All landing pages 404 | Dead — note in unfetchable list |
| `exhausted` | All strategies tried, mixed failures | Re-try with new strategies later |

The `policy_walled` bucket is the one to surface to OSTI support — it's where
DOE has the metadata but won't let us at the PDF, and that's the only fixable
class.

## Build sequencing (E.1 → E.2 → E.3)

Ship in waves so you can validate the platform before building exotic strategies:

- **E.1:** state DB + driver + osti_purl + unpaywall. Smoke 1K, then full
  pass. Should lift coverage ~46.5% → ~55%. Validates the platform.
- **E.2:** add crossref + s2 + arxiv. Re-run residual. Should lift to ~65%.
- **E.3:** add publisher_html per-recipe. Decide on google_scholar based on
  E.2 residual analysis — only build if marginal yield ≥1%.

Each wave is a distinct git tag; reporter at end of each wave produces the
per-strategy/per-lab/per-year matrix that informs the next wave's build
priorities.

## Failure modes this design prevents

- **Single-strategy plateau:** old `unpaywall_overnight.py` had no fallback;
  the 39.6% `skip_no_url` bucket was a dead-end. The chain converts it into
  the input for the next strategy.
- **Restart amnesia:** any prior implementation that didn't have a state DB
  would either re-fetch everything on restart (bad) or skip everything (also
  bad). `target_state` enum cleanly distinguishes done/exhausted/pending and
  the driver resumes correctly after SIGTERM.
- **Strategy poisoning:** without health monitor, a 503-ing Unpaywall would
  consume the entire residual queue marking everything as failed in 30
  minutes. Health monitor pauses the bad strategy, lets others continue.
- **Audit opacity:** every attempt logged means the reporter can answer "if I
  built strategy X, what's the marginal yield given residual?" which is the
  question that drives E.3 build decisions.

## Anti-patterns (don't do these)

- **Don't put the state DB on Cherry6TB or any USB-mounted/network volume**
  during the run. Snapshot to Cherry6TB nightly, that's it.
- **Don't run strategies sequentially per paper without parallelism** — driver
  should walk the chain serially per paper, but N papers in flight in
  parallel across worker threads.
- **Don't masquerade as a browser.** Polite pools want honest identity.
- **Don't skip the smoke-test phase.** 100-paper integration smoke before
  unleashing on 127K. The bug you find on row 50 of the smoke is the bug
  that would have wasted 4 hours on the full run.
- **Don't build google_scholar speculatively.** Captcha-walled, slow, and the
  marginal yield over arxiv + s2 + crossref is probably <1%. Only build if
  E.2 residual analysis justifies it.
- **Don't combine state-machine work with content extraction.** This pipeline
  stops at "validated PDF on disk." Marker OCR, classification, xCard
  extraction are all downstream — load `corpus-structured-extraction` for
  those layers.

## Reusable beyond OSTI

This pattern transfers cleanly to any "fetch N×10K records from heterogeneous
sources with mixed success rates" problem:

- arXiv corpus refresh (single source, but rate-limit + retry state still
  benefits from the schema)
- HuggingFace Hub bulk model/dataset card extraction
- Crossref/OpenAlex bulk metadata harvest
- Patent/clinical-trial corpus assembly

When forking, change: strategy list, rate limits, validate function (different
file magic), and `final_status` enum. Keep: schema shape, atomic claim,
driver loop, health monitor, reporter.

## Phase E plan source

Full task-by-task plan (38 tasks, 14 stages): `~/code/osti-replication-candidates/PHASE_E_PLAN.md`.
