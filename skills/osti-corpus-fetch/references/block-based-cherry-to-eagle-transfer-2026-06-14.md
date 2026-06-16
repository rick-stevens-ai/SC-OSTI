# Block-based cross-host transfer + GPU OCR pipeline (Cherry6TB → /eagle)

When shipping ~100K PDFs / ~300GB from Cherry6TB to ALCF `/eagle` for OCR (Marker, Nougat),
**block-based pipelining beats a single monolithic tar/rsync** for three reasons:

1. **Resumable on per-block boundaries** — a 38-min tar-pipe that dies at 16% has lost nothing recoverable; a block transfer keeps every completed block as a durable checkpoint.
2. **Overlapped transfer + compute** — block N transfers while block N-1 OCR's. With ~11 MB/s sustained WAN through cherryrd two-hop, total wall-clock drops from `transfer + compute` to roughly `max(transfer, compute)`.
3. **Bounded blast radius on Cherry6TB I/O lockup** — when Cherry I/O stalls (see below), the impact is a single 1500-PDF block stuck in TRANSFERRING, not the whole pipeline rolled back.

This reference captures the **block coordinator pattern** validated 2026-06-14 after a 38-min monolithic tar-pipe died with `Write error`.

## When to use this pattern

- Corpus to transfer is **>50GB and >10K files** to an HPC filesystem.
- Downstream compute (OCR, classification, extraction) can be queue-submitted per-batch.
- Transfer pipe is a 2-hop SSH through a bastion (cherryrd → polaris) with WAN limits ~10-20 MB/s.
- Source volume is **shared with other work** (your fetch daemon, your indexer, OS Spotlight, Time Machine) and can lock up unpredictably.

When you DON'T need this: corpus is <10GB, single-hop transfer, source volume is dedicated, downstream is a single batch job. Monolithic rsync is simpler and fine there.

## Failure mode this pattern avoids: monolithic tar-pipe `Write error`

Observed 2026-06-14 17:25 CDT, transferring `/Volumes/Cherry6TB/osti_corpus/_stage_flat/` (99,786 PDFs, 298GB) to `polaris-01:/eagle/.../staging_flat/`:

```bash
time (cd /Volumes/Cherry6TB/osti_corpus/_stage_flat && \
      tar -cf - . | ssh cherryrd "ssh polaris-01 'cd ... && tar -xf -'")

# 38m24.506s wall, exit 0 (the time wrapper swallowed it)
# tar: Write error
# 16,149 PDFs / 29GB landed (~16% by count, ~10% by size)
# No partial-resume capability — restarting tar would re-send all 16K files
```

Root cause is environmental — long-lived 2-hop SSH stream over hours hits transient network/timeout/buffer issues. The fix is not "tune tar more" — it's "stop running multi-hour single-stream operations."

## Block design — (year, lab) as the natural unit

For corpus organized by `(year, lab)` (OSTI/DOE shape), use that as the block boundary:

| Property | Value |
|---|---|
| Block ID | `B-YYYY-LAB`, e.g. `B-2020-ANL`, `B-2018-LBNL` |
| Oversize split | `B-YYYY-LAB-NN` when bucket >MAX_PDFS (e.g. `B-2020-ANL-01`, `-02`) |
| MAX_PDFS_PER_BLOCK | **1500** — yields ~3-9 GB per block, transferable in 5-15 min |
| Typical block count | 200-400 blocks for a 100K-PDF corpus |
| Largest single bucket observed | 2,149 PDFs (2020 ANL) — split to 2 blocks |
| Smallest single bucket observed | 1 PDF (rare year/lab pairs) — kept as own block |

The size cap is the key knob. Too small → too many ssh round-trips, too much queue overhead. Too large → block transfer time exceeds reasonable retry window, and one stuck block hurts more.

**1500 PDFs / ~5 GB / ~10 min per block** is the sweet spot for cherryrd 2-hop WAN.

## Coordinator state DB

```sql
CREATE TABLE blocks(
    block_id TEXT PRIMARY KEY,
    year INT, lab TEXT,
    n_pdfs INT, size_mb INT,
    status TEXT,                   -- PENDING | TRANSFERRING | TRANSFERRED | FAILED | SKIPPED_EMPTY
    transferred_n INT DEFAULT 0,   -- actual file count on remote after tar
    started_ts REAL, finished_ts REAL,
    error TEXT,                    -- short error string if FAILED
    osti_ids TEXT                  -- comma-joined OSTI IDs in this block
);
CREATE INDEX ix_status ON blocks(status);
```

Seeded once from a TSV manifest (built from the catalog with one SQL query grouping by year/lab + size-split). Then a worker picks PENDING blocks (size-ASC, smallest first for fast feedback) and ships them.

State transitions:
- `PENDING` → `TRANSFERRING` (worker claims)
- `TRANSFERRING` → `TRANSFERRED` (tar succeeded, file count verified on remote)
- `TRANSFERRING` → `FAILED` (tar nonzero rc, error captured)
- `PENDING` → `SKIPPED_EMPTY` (no source files exist on disk — usually a phantom catalog entry)

Idempotent: a re-run of the worker skips `TRANSFERRED` blocks. FAILED blocks can be reset to PENDING after diagnosis (`UPDATE blocks SET status='PENDING', error=NULL WHERE block_id=?`).

## Transfer mechanism — `tar` with `--files-from`, two-hop SSH

```python
# Build src file list
src_files = [f"{oid}.pdf" for oid in osti_ids if os.path.exists(f"{STAGE_LOCAL}/{oid}.pdf")]

# Ensure remote dir
ssh cherryrd ssh polaris-01 "mkdir -p /eagle/.../staging_blocks/<block_id>"

# Stream: local tar reads files-from-list, remote tar extracts
tar -cf - -C /Volumes/Cherry6TB/osti_corpus/_stage_flat -T - <<< "$file_list" \
  | ssh cherryrd "ssh polaris-01 'cd /eagle/.../staging_blocks/<block_id> && tar -xf - && ls | wc -l'"
```

Why `--files-from` instead of `tar -cf - <patterns>`: source dir has 99K files, explicit list avoids tar walking the dir (which itself hits Cherry I/O lockup risk). The list is the SQL row's `osti_ids` column, already in memory.

The remote `ls | wc -l` returns the actual landed file count to verify against the planned count.

## Pitfalls (validated 2026-06-14)

### Header row in TSV → shell pipeline injection

When reading the block manifest TSV in shell:

```bash
# WRONG — picks 'block_id' (the header) as a real block
SMALLEST=$(sort -t$'\t' -k4,4 -n /tmp/blocks.tsv | head -1 | cut -f1)

# RIGHT — skip header with tail -n +2
SMALLEST=$(tail -n +2 /tmp/blocks.tsv | sort -t$'\t' -k4,4 -n | head -1 | cut -f1)
```

Same rule for `awk` — use `NR>1` to skip header. Trivially avoidable, but burned 30s of confusion in this session.

### ControlMaster wedges `scp` while leaving `ssh` working

When 2-hop SSH transfers hang but `ssh cherryrd "hostname"` still works in 0.3s, the ControlMaster session is alive for command-channel but blocked for data-channel. Test:

```bash
# Test 1: plain ssh — usually fine
ssh cherryrd "echo alive"  # 0.3s

# Test 2: scp through ControlMaster
time scp /tmp/test.txt cherryrd:/tmp/probe.txt  # may hang 60s+

# Test 3: scp bypassing ControlMaster
time scp -o ControlPath=none /tmp/test.txt cherryrd:/tmp/probe.txt  # back to 0.3s
```

If test 3 works and test 2 doesn't, the master is wedged. Recovery: `ssh -O exit cherryrd` to terminate the master, next ssh creates a fresh one. Or just pass `-o ControlPath=none` for the transfer.

For long-lived block transfers, **always pass `-o ControlPath=none -o ConnectTimeout=10`** to avoid the wedged-master class entirely.

### Cherry6TB volume-wide I/O lockup (not just SQLite contention)

The prior consolidation reference notes "rsync starves SQLite queries." The broader lesson is: **Cherry6TB I/O can lock up the entire volume, not just specific operations.** Observed 2026-06-14:

- `ls /Volumes/Cherry6TB/` (root listing) — hung past 30s
- `dd if=/Volumes/Cherry6TB/osti_corpus/_stage_flat/873852.pdf bs=1M of=/dev/null` (single 856KB file) — hung past 60s
- No active rsync/tar/find/python processes from me at lockup time
- Plain `ssh cherryrd "echo"` worked instantly (so it's not network)

Likely contenders: `mdworker`/Spotlight, Time Machine, OS Cache eviction after large I/O burst, drive itself under stress from many small file accesses earlier in the session.

**Diagnostic ladder when Cherry I/O hangs:**

1. `ssh cherryrd "echo alive"` — rules out network/SSH.
2. `time ssh cherryrd "df -h /lus/eagle"` — rules out the destination side.
3. `time ls /Volumes/` (parent of Cherry mount) — does Mac filesystem even respond?
4. `time ls -la /Volumes/Cherry6TB/ | head -1` — does Cherry root respond?
5. `time stat /Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite` — does a single known-good `stat` work?
6. `time dd if=<small known file> of=/dev/null bs=1M count=1` — can we read at all?
7. `ps auxww | grep -E "mdworker|backupd|cloudd|spotlightd"` — known background contenders.
8. Check Activity Monitor (or `top -o cpu`) for any process at high I/O.

If all the Cherry diagnostics hang together but cherryrd/polaris work, the volume is genuinely stuck — wait 5-10 min or unmount/remount. Don't keep retrying transfers against a locked volume; they'll cascade into more stuck processes.

**Mitigation for block coordinator:** the coordinator's `os.path.exists` and `os.path.getsize` calls happen per-block, not once at startup. A locked Cherry pauses all workers but doesn't crash any of them. When Cherry returns, the next worker iteration resumes.

### `tar -cf - . | ssh ... 'tar -xf -'` swallows the real exit code

When the receiving tar fails, the sending tar gets SIGPIPE and exits nonzero — but if the whole thing is wrapped in `time (...)` or `bash -c '...'` the exit code reported is often 0. Always capture both exit codes:

```python
tar_local = subprocess.Popen(['tar', '-cf', '-', '-T', '-'], ...)
tar_remote = subprocess.Popen(SSH + ['tar -xf -'], stdin=tar_local.stdout, ...)
tar_local.stdout.close()
out, err = tar_remote.communicate(timeout=1800)
tar_local.wait()
if tar_remote.returncode != 0 or tar_local.returncode != 0:
    # FAILED — record both rc values
```

The `tar: Write error` line goes to stderr of the local tar, which is often closed/redirected. The recv side reports clean "closed" and exits 0. The only reliable signal that data didn't fully arrive is **counting files on the remote** and comparing to planned count.

### Don't run pre-flight enumeration of source dir when launching from a contended volume

```bash
# DANGEROUS pre-flight in transfer script header:
echo "Source: $(ls $SRC | head -3 | wc -l) sample dir entries"

# This `ls` of /Volumes/Cherry6TB/osti_corpus/_stage_flat/ (99K entries)
# can hang for 60s+ on first run after a Cherry I/O lull. Strip it.
```

Trust the catalog's row count. The `os.path.exists` per-file check inside the transfer loop is bounded and gives the same information.

## Cross-link to other patterns

- `references/multi-source-consolidation-2026-06-13.md` — the prior step (consolidate Cherry sources to single canonical layout). This block-transfer reference is the **next** step (ship the consolidated corpus to HPC for OCR).
- `references/coverage-accounting-2026-06-10.md` — the discipline that makes the catalog trustworthy as a planning source (so block manifests reflect reality, not stale state).

## Generalizes to

- Any "ship N×GB to HPC for batched compute" pattern where transfer pipe is slow and source filesystem is shared/contended.
- arXiv corpus → ALCF for full-text classification jobs.
- Model checkpoint distribution from a build node to multiple GPU nodes.
- Patent corpus → cluster for NER extraction.

The shape: **state DB of work units, per-unit atomic transfer with verified landing count, idempotent worker that resumes from any partial state, decouple transfer-throughput from compute-throughput so neither blocks the other.**
