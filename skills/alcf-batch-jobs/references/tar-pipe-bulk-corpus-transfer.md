# Tar-pipe over two-hop SSH for bulk corpus transfer to ALCF

When you need to push tens of thousands of small files (~99K PDFs, ~298 GB total) from a local volume to an ALCF project filesystem, **rsync is the wrong choice** — per-file overhead dominates over a multi-hop SSH path and you'll cap at ~13 MB/s regardless of link bandwidth. The right primitive is a tar pipe.

Verified 2026-06-14: m1 → cherryrd → polaris-02, 1199 files / 994 MB landed in ~60s (~17 MB/s) on the first minute of a 99,787-file / 298 GB push. That's 1.3× rsync's throughput on the same data — and rsync's number was AFTER 30+ minutes of warmup; tar-pipe holds that rate steady.

## The recipe

```bash
# Source: flat dir on m1 (e.g. /Volumes/Cherry6TB/osti_corpus/_stage_flat/ with 99K *.pdf hardlinks)
# Target: empty dir on polaris (made beforehand)
ssh cherryrd "ssh polaris-02 'mkdir -p /eagle/projects/<proj>/<user>/<target>'"

# Background the transfer (Hermes background=true), notify-on-complete
time (cd /Volumes/Cherry6TB/osti_corpus/_stage_flat \
   && tar -cf - . \
    | ssh cherryrd "ssh polaris-02 'cd /eagle/projects/<proj>/<user>/<target> && tar -xf - && echo DONE && ls | wc -l'")
```

Single quoted-shell at each hop. The producing tar streams archive frames to stdout, which ssh forwards directly to the consuming tar on the destination. No temp files, no per-file SSH session overhead.

## Common pitfalls

### 1. Don't insert `pv` unless it's installed on the source

`tar -cf - . | pv -s 320G | ssh ...` looks helpful but fails silently with `bash: pv: command not found` (on macOS without `brew install pv`), pv exits, pipe closes empty, receiving tar reports `tar: This does not look like a tar archive` — and you blame the transport when the actual error is missing pv on the producer.

**Fix:** install pv first, OR omit it entirely. To get progress without pv, poll the destination from a separate parent-session call:

```bash
ssh cherryrd "ssh polaris-02 'du -sh /eagle/.../target; ls /eagle/.../target | wc -l'"
```

### 2. Hermes m1→cherryrd SSH has soft ~60s session timeout

For any tar pipe that may run >60s (i.e. >~1 GB on this path), use `terminal(background=true, notify_on_complete=true)` to dispatch the transfer. Otherwise the m1-side ssh dies mid-pipe and you ship a partial archive that the receiving tar accepts as truncated success — silent corruption.

### 3. Receiving tar's "not a tar archive" error has TWO common causes

- Producer pipe closed empty (pv-missing as above; ssh died; stdin closed before tar wrote anything).
- Archive transmission corrupted (rare on SSH; treat as the second hypothesis only).

Diagnose by running the producer-side `tar -cf - . | wc -c` locally first to confirm tar produces bytes, then add the SSH pipe.

### 4. Don't use `rm -rf` to clear the target without confirming source-vs-dest

Tar-pipe target dirs should be freshly-made empty dirs. If you `rm -rf /eagle/.../existing-data && mkdir ...` to "clean up," verify you're rming the staging dir, not the canonical mirror. Two-hop SSH + bash + quoting is friendly to typo-driven mass deletion. Always echo the target path first:

```bash
ssh cherryrd "ssh polaris-02 'echo TARGET=/eagle/projects/<proj>/<user>/<target>; rm -rf /eagle/projects/<proj>/<user>/<target> && mkdir -p /eagle/projects/<proj>/<user>/<target>'"
```

## Throughput baseline

| Path                                    | Mechanism        | Throughput    | Notes                                                   |
|-----------------------------------------|------------------|--------------|---------------------------------------------------------|
| m1 → cherryrd → polaris-02 (~99K files) | rsync            | ~13 MB/s     | Per-file overhead dominates                             |
| m1 → cherryrd → polaris-02 (~99K files) | tar-pipe         | ~17-30 MB/s  | Steady; ETA ~3-5h for 298 GB                            |
| m1 → cherryrd (single file >1 GB)       | rsync            | ~25-30 MB/s  | Decent for big single files                             |
| m1 → cherryrd (single file >1 GB)       | scp              | ~30 MB/s     | Equivalent to rsync for one big file                    |
| cherryrd → polaris-02 (cherryrd-local source) | rsync       | ~50-80 MB/s  | Skip the m1 hop when source is already on cherryrd      |

**When the source is already on cherryrd**, two-hop becomes one-hop and rsync gets back into the game. The tar-pipe pathology specifically hits when many small files are being read off macOS-side disk through two SSH hops.

## When NOT to use tar-pipe

- Source has a few large files. rsync handles those fine and gives you progress per-file.
- You need partial-progress resumability. tar-pipe is all-or-nothing; if it dies at 50%, you start over (or rsync the diff). For a 298 GB transfer that may take 4-6h, consider chunking by year-subdir and running tar-pipes per-chunk so a partial recovery doesn't restart everything.
- Source has online-stub files (Dropbox stubs, iCloud placeholders). tar will materialize them, which may be wrong intent. Verify with `find <src> -size +1c | wc -l` vs `find <src> | wc -l` before transferring.

## After the transfer

1. **Count check:** `ssh cherryrd 'ssh polaris-02 "ls /eagle/.../target | wc -l"'` should match source count.
2. **Size check:** `du -sh` both sides; should agree within 1-2% (filesystem overhead differs).
3. **Spot SHA check:** pick 5-10 files, sha256sum on both sides, confirm match.
4. **DON'T delete the source staging dir** until the manifest builder has run against the destination and you've validated all expected rows landed with correct sizes.
