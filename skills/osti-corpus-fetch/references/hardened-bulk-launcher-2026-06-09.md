# Hardened bulk-fetch launcher pattern (2026-06-09)

Distilled from the OSTI `failed_recovery.txt` re-fetch staging on 2026-06-09. This pattern applies to **any bulk re-fetch over an 8K+ ID pool** where you need ironclad provenance, predictable per-request budgets, and a hard gate between "I built the launcher" and "I committed N hours of network egress."

## The four-stage sequence

The discipline is **build → dry-run → pilot → full subset**, each gated, each producing artifacts that survive the next stage. Don't compress stages.

| Stage | Output | Gate to next stage |
|---|---|---|
| 1. Build launcher + wrapper | `osti_bulk_fetch.py`, `wrapper.sh`, `failed_recovery_tagged.jsonl` | Code review by Rick, all locked rules implemented |
| 2. Dry-run | `dryrun_v2/MANIFEST.json` (settings snapshot, per-lab distribution, wall estimate) | Distribution makes sense (no surprise pool composition), wall estimate fits the window |
| 3. Pilot (~500 IDs) | `results.jsonl` from pilot run | Recovery rate ≥ probe baseline, no terminal-bucket surprises, no rate-limit signals |
| 4. Full recoverable subset | `results.jsonl` from full run | — |

**The gate that gets skipped under pressure is stage 2.** Always run the dry-run before shipping the launcher to the remote host. The MANIFEST.json file is the canonical record of what the launcher WOULD have done — keep it.

## Launcher non-negotiables (locked rules)

These are baked into `osti_bulk_fetch.py` and you should NOT relax them without explicit Rick approval. Most were learned by hitting the failure mode once.

1. **Streaming download + %PDF magic-byte check.** Don't trust Content-Type. OSTI returns `text/html; charset=utf-8` for the canned "not available" page, but Content-Type can be set wrong on real PDFs too. Read the first 4 bytes, require `%PDF`, then stream the rest.
2. **`NOT_PDF_HTML_4231_SIZE = 4231` is a first-class terminal bucket.** All instances of this bucket are byte-identical: `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=devi…`. It is OSTI's deterministic "no fulltext available" response. Never retry. Classify it separately from `not_pdf` so it doesn't pollute the retry-worthy bucket.
3. **`TRANSIENT_BUCKETS = {"reset", "timeout"}` are the ONLY retry-worthy buckets.** Backoff `[1, 3, 9]` seconds, max 4 attempts. Everything else (`http_403`, `http_404`, `not_pdf_html_4231`, `not_pdf`, `oversize_pdf`, `oversize_non_pdf`) is terminal-no-retry — those need investigation or escalation, not bandwidth.
4. **Per-stage timeouts.** Meta 10s, landing 10s, PDF body 60s. The PDF stage is generous because large PURL responses on slow paths can legitimately take 20-30s; the meta/landing stages should be snappy or something is wrong.
5. **Per-request polite floor 8s, hard cap 3 Hz.** Sequential single-thread for OSTI — never parallel. Aurora UAN is a shared resource; respect it.
6. **Cap = 100 MB default.** 50MB false-rejects ~3% of legitimate large PDFs (the 2026-06-09 probe hit ID 1992474 at 52.5MB). 100MB is the floor; don't go lower.
7. **Append-only JSONL checkpoint keyed by `osti_id`.** Pre-load existing checkpoint on launch; skip already-attempted IDs. Survives interruption + restart with no idempotency hazard.
8. **Deferred-exclusion list with `--include-deferred` override.** Hard-coded labs that the probe proved 0% recoverable get excluded by default. The override exists for "we want to confirm the wall is still up" runs, not for production bulk fetches.

## Wrapper contract (`wrapper.sh`)

The wrapper exists to make the launcher's lifecycle observable from outside the running process. Three sentinel files:

- `wrapper.start` — written ISO timestamp + cmdline BEFORE the python import latency. If this is missing, the wrapper itself didn't fire.
- `wrapper.pid` — current PID; cleared on EXIT trap.
- `wrapper.status` — `"RUNNING"` while alive, `"EXIT <code>"` after exit (trap-on-EXIT, fires even on signal).
- `wrapper.end` — ISO timestamp + exit code + duration.
- `wrapper.log` — full stdout+stderr, line-buffered (`PYTHONUNBUFFERED=1` + bash `stdbuf -oL -eL`).

The contract: **a polling reader (you, an hour later) can determine launcher state without attaching to the process.** `cat wrapper.status` says it all.

Why this matters with two-hop SSH: m1's terminal tool has a ~60s implicit timeout on the outer ssh wrapper. If you start the launcher with bare `ssh aurora "python ..."`, the SSH session dies at 60s and you have no clean way to know whether the launcher continued. The wrapper pattern is what makes it safe to fire-and-poll across two-hop SSH.

## Pilot-shape decision

When the manifest is heavily lab-skewed (e.g. 92% LBNL), there are two pilot shapes:

- **A. Straight `--limit 500`** — first 500 IDs from the manifest in their natural order. Result is LBNL-dominant if the manifest is LBNL-dominant.
- **B. Hand-stratified across labs** — build a smaller manifest with ~100 each across the major labs.

**Pick A by default.** The purpose of the pilot is to gate the full run; you want the pilot to be representative of the full run, not "fair" across labs. The probe stage already established per-lab recoverability — the pilot is measuring whether **the launcher mechanics** (rate limiting, retry behavior, checkpoint integrity, PDF validation) work at 500-ID scale before committing to 8,000+. LBNL-dominant pilot is the right shape if LBNL is 92% of the actual run.

Only pick B if the dominant lab has a known structural issue you specifically want to avoid front-loading (e.g. they rate-limit aggressively after 200 sequential requests).

## Join provenance — dry-run must surface it explicitly

When the manifest is built by **joining** a missing-IDs file against a separate metadata source (e.g. `failed_recovery.jsonl` ⋈ `recon_v2/*.jsonl` to attach lab tags), the dry-run output must surface the join — not just the result. The launcher knowing the lab field is not the same as the operator being able to audit how the lab field got there.

Surface, in dry-run mode, all of:

- **Source file for missing IDs** with row count.
- **Source file/dir for the join attribute** (e.g. `recon_v2/<Lab>__<year>.jsonl`) with file count and unique-pair count.
- **Join key** (e.g. `osti_id` string → `_query_lab` field).
- **Lab-name normalization map** if you collapsed names. If you didn't normalize (recon_v2 strings used verbatim), say so — "NONE" is a valid and important answer.
- **Matched count vs total** after the join.
- **Unmatched count** (the UNKNOWN bucket) and how the launcher treats them — excluded by default with an `--include-unknown` override, or included with a warning. Either is defensible; **silent inclusion is not**.
- **Excluded counts after the join**: deferred-lab exclusion AND unknown-lab exclusion, separately, so the operator can see where each subtraction came from.

The failure mode if you skip this: the launcher consumes the tagged manifest correctly, the dry-run shows a clean per-lab distribution, and the 11 unjoined IDs slip through into the pilot — failing in ways that look like rate-limit anomalies because they have no lab context to correlate against. Rick caught this 2026-06-09: "join lab/year from the recon_v2 audit, not from the missing-ID JSONL ... make the join explicit in the plan output ... if any IDs don't join cleanly, keep them in an unknown_lab bucket and don't fetch them in the first pilot unless we explicitly include them."

Reference implementation (verified 2026-06-09 dry-run):

```
  --- join provenance ---
  missing-IDs source:    /path/to/failed_recovery.jsonl  (8707 rows)
  id->lab source:        /path/to/recon_v2/  (210 <Lab>__<year>.jsonl files, 407,704 unique osti_id->lab pairs)
  join key:              osti_id (string) -> _query_lab field in recon_v2
  lab-name normalization: NONE — recon_v2 _query_lab strings used verbatim (10 distinct lab strings, all canonical)
  matched after join:    8696 / 8707
  unmatched (UNKNOWN):   11  (excluded by default)
  ...
  deferred-excluded:         15  (excluded: ['Fermi National Accelerator Laboratory', 'Thomas Jefferson National Accelerator Facility'])
  unknown-excluded:          11  (excluded by default: IDs not joined to recon_v2)
  TO RUN:                  8681
```

This generalizes beyond OSTI: **any bulk-fetch launcher whose manifest is enriched by an upstream join must surface the join in dry-run**. The same shape applies to arXiv ID lists joined against OpenAlex metadata, HF Hub model lists joined against a curated taxonomy, etc.

## Pool-composition surprises

The 2026-06-09 manifest came in at **92% LBNL** (7,969 of 8,707 IDs). The stratified 38-ID probe (which was for fairness — 4 per lab — not representativeness) had created a mental model where the lab mix was relatively even. When the real distribution dropped, both the pilot shape and the email-v2 framing changed:

- **Pilot shape** stays straight-A even more confidently — stratifying away from the 92% is the wrong move.
- **Email framing** must reflect the actual rescue math: excluding Fermi+JLab (the host-independent walls) saves only **15 IDs** (13 Fermi + 2 JLab) out of 8,707, NOT the "500-700" that earlier intuition suggested. The exclusion is still correct on outcome-quality grounds — those IDs return the canned 4231-byte HTML deterministically — but the framing of "scope reduction" in any external email needs to use the real number, not the wishful one.

The lesson: **before drafting an email or making a scope decision based on the probe sample, recompute the per-bucket counts against the actual manifest distribution.** Probe-derived numbers don't transfer; manifest-derived numbers do.

## Mirror-scope discipline (cross-host)

When you mirror canonical artifacts to a remote host's working directory (e.g. Aurora `/home/stevens/osti_probe/`), keep the mirror **single-workflow scoped**. The OSTI probe directory should contain only OSTI artifacts: probe rundir, summary card, sample TSV, launcher when staged, scripts. Adjacent vault cards (e.g. host-specific cards like `chiatta00.md`) belong in a different mirror or stay on m1.

The failure mode if you don't enforce this: someone (you, Ollie, Rick) opens the mirror dir later to debug an OSTI issue, sees an unrelated card, has to figure out whether it's part of the OSTI artifact set, possibly acts on it as if it were. Mixing scopes erodes the "I can trust this directory's contents are all part of one workflow" property that makes mirrors useful.

Rule: **one mirror dir = one workflow.** When in doubt about whether a file belongs, ask the owner before adding it. Stray files get cleaned up; missing files get rsynced fresh.

## Canonical paths (this run)

- Launcher dir: `~/code/osti-replication-candidates/bulk_launcher/`
  - `osti_bulk_fetch.py` (18,522 bytes, 100MB cap default)
  - `wrapper.sh` (2,809 bytes, +x)
  - `failed_recovery_tagged.jsonl` (8,707 rows, lab-tagged via recon_v2 join)
  - `dryrun_v2/MANIFEST.json` (settings + per-lab distribution + wall estimate)
- Probe artifacts (frozen, post-correction canonical):
  - m1: `~/code/osti-replication-candidates/probe_uan_20260609-034442/SUMMARY.md`
  - Aurora mirror: `/home/stevens/osti_probe/probe_uan_20260609-034442/SUMMARY.md`
- Vault card (OSTI-only, no QE references): `~/Dropbox/XFER/memory-vault/workflows/osti-corpus-refresh.md`

## Cross-host shipment recipe

Direct `scp aurora:` from m1 does NOT work — `aurora` alias isn't resolvable from m1's ssh config. Use the two-hop via cherryrd.

### Target directory: Flare project space, NOT `/home`

**`/home/stevens` on Aurora is quota-capped at 150G** (verified 2026-06-09 via `lfs quota -uh stevens /home`: 51.72G used, 150G quota, 165G limit). Large staging payloads — anything ≥10s of MB total, like the 55MB sidecar map plus the launcher payload — will hit the quota during write even when `df -h` says 14P/12P free on `/home`. The transient state during partial transfers (half-written file from a failed scp still consuming space) can push you over even if the final payload would fit.

**Stage to Flare project space instead:** `/lus/flare/projects/AuroraGPT/stevens/osti_bulk/`. Flare is 91PB / 29PB free, no per-user quota, and is where the QE work already lives so it's the established working area.

### Use rsync, not scp, for multi-file pushes

scp's failure mode for two-hop transfers with at least one large file is **silent partial truncation with misattributed error messages**. When the destination filesystem rejects a write mid-stream (quota, disk full, network blip), scp can:

- Leave the target file at a smaller size than the source (e.g. 4MB on disk for a 55MB source) with HTTP-200-equivalent exit semantics on the small files in the same invocation.
- Print "scp: failed to upload" errors that name the WRONG files (the small ones that actually transferred fine), because the broken control connection bleeds onto subsequent ops in the same multiplexed session.

You will not notice unless you SHA256-verify. The launcher will then dry-run cleanly (it doesn't validate the sidecar's hash matches its `.SHA256` companion at load time) but produce garbage lab tags on real records.

**rsync handles this correctly**: resumes on partial transfer, reports per-file outcome accurately, prints "Disk quota exceeded" cleanly so you know the real failure cause.

### Recipe (verified 2026-06-09)

```bash
# 1. Stage on cherryrd first
scp ~/code/osti-replication-candidates/bulk_launcher/{osti_bulk_fetch.py,wrapper.sh,README.md,failed_recovery_tagged.jsonl,osti_id_lab_year_map.jsonl,osti_id_lab_year_map.SHA256} \
    cherryrd:/tmp/osti_bulk_staging/

# 2. Pre-create target on Aurora (Flare project space, not /home)
ssh cherryrd 'ssh aurora "mkdir -p /lus/flare/projects/AuroraGPT/stevens/osti_bulk"'

# 3. rsync from cherryrd to Aurora (resumable, accurate error reporting)
ssh cherryrd 'rsync -av /tmp/osti_bulk_staging/ aurora:/lus/flare/projects/AuroraGPT/stevens/osti_bulk/'

# 4. MANDATORY: verify SHA256 of the sidecar map on Aurora matches m1 source
ssh cherryrd 'ssh aurora "sha256sum /lus/flare/projects/AuroraGPT/stevens/osti_bulk/osti_id_lab_year_map.jsonl"'
# Compare against the .SHA256 companion file or the m1 source hash.
# If mismatch: re-rsync, do NOT proceed to dry-run.
```

### Aurora Python pinning

Default `/usr/bin/python3` on `aurora-uan-0009` is **3.6.15** — too old for f-string `=` debug expressions, walrus operator, modern typing. Use `/usr/bin/python3.10` explicitly for any dry-run or pilot launch. `wrapper.sh` already pins `PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.10}"` — direct invocations of the launcher (e.g. for dry-runs) must pin manually.

### Wording-vs-behavior audit (before pilot)

When the launcher has evolved through multiple patches — especially when a sibling subagent (parallel session on cherryrd, or a prior context window) was editing the same file — **audit that prose, constants, and emitted strings all agree** before declaring "ready for pilot." The behavior is usually correct because it's driven by the code; the wording lags because docstrings, README, and dry-run print statements live in different parts of the file.

The four places that must agree:

| Place | What to check |
|---|---|
| `osti_bulk_fetch.py` module docstring + argparse `default=` + bucket constants | Cap value, retry-count semantics ("3 retries / 4 total" vs "3 total"), terminal-bucket names |
| `wrapper.sh` | START/EXIT trap, `wrapper.{pid,status,start,end,log}` sentinels present, `PYTHONUNBUFFERED=1`, `PYTHON_BIN` pinned to `/usr/bin/python3.10` |
| Dry-run MANIFEST.json | `settings.cap_mb`, `settings.max_attempts`, `settings.retry_schedule_sec`, `settings.terminal_buckets_no_retry`, `settings.deferred_labs_excluded_by_default`, `paths.id_lab_map_sha256` |
| Vault card / SUMMARY.md | Cap value, terminal-bucket names, host name, pilot-first path |

Cheap repeatable audit script: enumerate ~30-40 boolean checks across all four files, run them all, report pass/fail per check. Cost is ~2-3 tool calls; benefit is catching the kind of wording drift Ollie called out in the 2026-06-09 dry-run (RETRY_SCHEDULE comment said "3 attempts total" while behavior was actually "initial + 3 retries = 4 total"). The wording mismatch isn't a bug in behavior but it IS a bug in operator trust: if the manifest says `max_attempts: 4` and the comment says "3 attempts total," the operator can't tell which is true without reading the loop.

**Naming convention for the retry semantics:** describe it as "4 total attempts max (initial + 3 retries) with backoff [1, 3, 9]" — both halves. Ambiguous phrasings like "3 attempts" or "3 retries" are operator traps because reasonable readers disagree on whether the initial attempt counts.

**Recovery when the audit finds drift:** patch every wording site in one pass (module docstring, RETRY_SCHEDULE comment, dry-run print, README, vault card if it mirrors the same number), then re-stage **only the changed files** to the remote — not the whole bundle. The sidecar map (largest payload) doesn't change for wording fixes, so re-pushing it wastes minutes and re-introduces SHA-mismatch risk if the network blips. Verified 2026-06-09: 2-file rsync (`osti_bulk_fetch.py` + `README.md`, ~26KB total) took <1s and the Aurora dry-run reflected the wording fix immediately.

### Sibling subagent edit-drift on shared files

When working on a launcher that's being co-edited by a parallel session (sibling subagent on a different host, or Ollie on cherryrd), the `patch` tool will surface a warning: `"<file> was modified by sibling subagent '<id>' but this agent never read it."` **Take the warning seriously.** The sibling may have added arguments, renamed fields, or shifted line numbers in ways that invalidate your mental model of the file.

The recovery is mechanical:
1. Re-read the file (`read_file` or `terminal cat`) before making more edits.
2. Diff your mental-model of the function/argument signatures against what's actually in the file (`grep -n '^def \|add_argument'` is usually enough).
3. If the sibling renamed an argument (e.g. `--out` → `--outdir`), update any README/wrapper that references the old name in the same patch pass so they don't diverge further.

The 2026-06-09 case: a sibling subagent on cherryrd had renamed `--out` to `--outdir`, added `--include-unknown`, and added a sidecar-map argument while my m1 session was running. The patch tool warned on the next edit; reading the file caught the rename before I shipped a README that would have referenced a nonexistent flag. **Do not assume the file is what you last left it** on any launcher that's co-owned across sessions.

### Two dirs, two workflow scopes

- `/lus/flare/projects/AuroraGPT/stevens/osti_bulk/` — launcher payload (script, wrapper, manifest, sidecar map, README). Re-stageable from m1.
- `/lus/flare/projects/AuroraGPT/stevens/osti_bulk_pilot/` — pilot output (results.jsonl, pdfs/, launcher.log, MANIFEST.json, wrapper.* lifecycle files). Created by the pilot run, not by staging.
- `/home/stevens/osti_probe/` — older probe artifacts (frozen). Don't add to.

Keep them separate per mirror-scope discipline above.
