# Cherry6TB → ALCF /eagle bulk transfer (lessons from 2026-06-14)

When you need to ship the consolidated `osti_corpus/_stage_flat/` (or any 100K+ small-file
directory) from Cherry6TB on m1 to `/eagle/projects/<proj>/stevens/...` on Polaris, the
naive choices each have a sharp edge. This is the playbook.

## Pattern: two-hop via cherryrd

m1 → cherryrd → polaris-01. Direct m1 → polaris would also work, but routing via cherryrd
inherits the existing ALCF auth + ControlMaster session and avoids re-authenticating from
m1. The downside is **cherryrd's WAN is the bottleneck** (~11-17 MB/s sustained for
small-PDF workloads), not Cherry6TB nor /eagle nor Polaris.

## Choice 1 (rejected): single tar pipe with time(...) wrapper

```bash
# DON'T DO THIS for >30min transfers
time (cd $SRC && tar -cf - . | ssh cherryrd "ssh polaris-01 'cd $DST && tar -xf -'")
```

**Why it bites:** if the ssh session drops or polaris-side tar gets a Write error
mid-stream, `tar -cf -` on the source side dies with a broken pipe, exit code of the
**subshell** still propagates through `time` as 0 (because `time` reports the wall-clock,
not the inner exit code, and the pipefail behavior of the assembled command is murky
across mac/bash interaction). You see `real 38m24s` and assume it completed; the
process notifier fires "completed exit 0"; only when you check the destination do you
discover **29G / 16,149 PDFs of 298G** landed and the rest is silently missing.

Symptoms in the log to watch for:
- `tar: Write error` mid-stream (real error)
- `tar: Ignoring unknown extended header keyword 'LIBARCHIVE.xattr.com.apple.provenance'`
  is HARMLESS — macOS xattr metadata, ignore.

## Choice 2 (preferred): rsync --ignore-existing as resume primitive

```bash
/opt/homebrew/bin/rsync \
  -a \
  --ignore-existing \
  --partial \
  --timeout=300 \
  --stats \
  -e "ssh cherryrd ssh" \
  /Volumes/Cherry6TB/osti_corpus/_stage_flat/ \
  polaris-01:/eagle/projects/AuroraGPT/stevens/osti_mirror/staging_flat/
```

Key flags:

| flag | why |
|------|-----|
| `--ignore-existing` | skip files already at dest by name (size not checked) — what you want for resumed transfers of immutable PDFs |
| `--partial` | keep partially-transferred files if killed; next run finishes them |
| `--timeout=300` | 5min idle timeout cleanly kills hung sessions vs hanging forever |
| `-e "ssh cherryrd ssh"` | the two-hop magic — rsync's `-e` accepts any command; "ssh cherryrd ssh" becomes the transport that opens an ssh session through cherryrd to the next hop |
| `-a` not `-av` | skip verbose per-file logging on 100K files (log noise + slows the wrapper) |

**Resume behavior**: after the failed tar got 16,149 PDFs across, `rsync --ignore-existing`
starts by enumerating the source (slow, 1-2 min on Cherry6TB I/O contention), then
enumerating the dest (fast on /eagle), then transferring only the diff.

`/opt/homebrew/bin/rsync` not `/usr/bin/rsync` — system rsync on macOS is the old 2.6.9
that lacks `--info=progress2` and other modern flags. Use the homebrew 3.4+ binary.

## Choice 3 (acceptable but slower): chunked-tar with sequence number

Split the source into N tar chunks, ship sequentially, resume by skipping the chunks
already on dest. More complex, but each chunk's completion is atomically verifiable. Use
this only if `--ignore-existing` is too slow because the source enumeration phase is the
bottleneck (it isn't usually — Cherry6TB walk of `_stage_flat` is ~30s).

## Pre-flight: never `ls` the source dir on Cherry6TB

```bash
# DON'T — hangs 60s+ on HFS catalog contention
ls /Volumes/Cherry6TB/osti_corpus/_stage_flat/ | wc -l

# DO — read from the catalog SQLite snap instead
python3 -c "import sqlite3; print(sqlite3.connect('/tmp/cat5.sqlite').execute('SELECT COUNT(*) FROM papers WHERE has_pdf=1').fetchone()[0])"
```

The pre-flight `ls` is the most common "rsync wrapper script hangs at 0 bytes transferred"
cause when Cherry6TB has other I/O in flight. **Strip diagnostic `ls`/`find -maxdepth 1`
from any bulk-transfer wrapper script** — get the count from the catalog DB, not the
filesystem.

## Quoting through three layers

The `tar`/`rsync` over `ssh cherryrd 'ssh polaris-01 "<cmd>"'` pattern has brutal quote
escaping. The pattern that works:

```bash
ssh cherryrd "ssh polaris-01 '<single-quoted cmd>'"
```

- Outer double quotes (let `$VARS` expand on m1 if needed)
- Middle single quotes (literal payload to polaris)
- No nested double quotes inside the single-quote payload — they collide with the outer

If the payload needs `$VARS` to expand on polaris (not m1), use HEREDOC pattern:

```bash
ssh cherryrd 'ssh polaris-01 << EOF
cd /eagle/projects/...
COUNT=$(ls | wc -l)
echo "got $COUNT files"
EOF'
```

## Throughput expectations

| workload | sustained MB/s | notes |
|----------|---------------|-------|
| Many small PDFs (~100KB each), tar-pipe via cherryrd | 11-17 | cherryrd WAN bound |
| Many small PDFs, rsync via cherryrd | similar | per-file overhead modest |
| Direct rsync m1 → polaris-01 (no cherryrd hop) | 20-30 | when ALCF auth from m1 is current |
| Single large file (.sqlite snap) m1 → cherryrd | 25-50 | LAN throughput, no per-file overhead |

For 298GB of small PDFs at ~12 MB/s, plan **~7 hours**. Run as background process with
`notify_on_complete=true`. Don't poll every 5 minutes — let the notification fire.

## Polaris login node selection

Polaris has 4 login nodes (polaris-01 through polaris-04). The `polaris` alias on
cherryrd may route to a stale ControlMaster (verified 2026-06-14: `polaris` routed to
`polaris-login-04` with a stale master, session attach hung; `polaris-02` worked
directly). **Always pin a specific login node when scripting** — `ssh polaris-01` or
`-02`, not `ssh polaris`. If one is auth-stale, the others usually still have a valid
session.

After auth expires (Polaris CRYPTOCard OTP is short-lived), all login nodes will need
re-auth — Rick has to punch the OTP on cherryrd manually. No way around this from a
session.

## Manifest base path discipline

The transfer destination MUST match the manifest base path. Two project bases exist on
/eagle that Rick has access to:

- `/eagle/projects/AuroraGPT/stevens/` — main AuroraGPT alloc, where `osti_marker/` and
  `osti_mirror/` actually live
- `/eagle/projects/argonne_tpc/stevens/` — does NOT exist; was a transcription error in
  a prior summary

**Pre-flight check before launching transfer**:

```bash
ssh cherryrd "ssh polaris-01 'ls /eagle/projects/AuroraGPT/stevens/osti_mirror/'"
# should show: pdfs/ md/ mmd/ other/ manifest/ logs/ scripts/ staging_flat/ reconcile/
```

If the dir is missing, recreate the skeleton first (`mkdir -p` the 28 × 11 × 4
year/lab/kind buckets) before starting transfer — or the tar receiver will create
unintended directory structure.

## Mid-session "wrong path" panic — verify before declaring crisis

Session 2026-06-14 had a brief panic: I read in the summary that the skeleton was at
`argonne_tpc` and the tar pipe was writing to `AuroraGPT`, looked like a serious bug.
Reality: the summary was sloppy on my end; `AuroraGPT` was the real path, the skeleton
was already there, the tar was writing to the correct location. **Verify with `ls` before
declaring a crisis when paths look wrong** — costs 5 seconds, saves declaring a problem
that doesn't exist.

## Cross-reference

- `references/multi-source-consolidation-2026-06-13.md` — building `_stage_flat` in the
  first place (Stages 0-4 consolidation that produces the directory you're now shipping)
- Skill `kukla-self-operations` — Polaris auth + OTP discipline + ALCF two-hop pattern
- Memory: "ALCF two-hop pattern: ssh cherryrd 'ssh polaris-02 \"<cmd>\"'. Quote escaping
  is brutal." — this reference expands that into a working tar/rsync recipe
