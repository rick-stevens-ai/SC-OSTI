# Cherry6TB Spotlight wedge — cascade to shell session

**Date observed:** 2026-06-14
**Symptom escalation:**
1. `dd if=/Volumes/Cherry6TB/osti_corpus/_stage_flat/873852.pdf of=/dev/null bs=1M` → 60s timeout
2. `ls /Volumes/Cherry6TB/` → 30s timeout
3. `mdutil -s /Volumes/Cherry6TB` → 30s timeout
4. **`echo alive` in a fresh terminal call → timed out**
5. `search_files` over `/Users/stevens` (not Cherry6TB!) → 60s timeout

By stage 4 the agent had **no working shell layer** even with `workdir=/tmp` set. Every command timed out before producing a single byte of output. This was NOT a tool-stack bug — it was a real kernel-side wedge that propagated.

## What's actually happening

macOS Spotlight (`mds_stores` / `mdworker_shared`) was reindexing Cherry6TB's HFS+ catalog. Cherry6TB holds 100,000+ small PDFs in `osti_corpus/_stage_flat/` (hardlinks to the canonical store) — HFS+ catalog scan over that many sibling inodes is pathological. While the indexer holds the HFS+ catalog lock, **any inode lookup on the volume blocks**.

The persistent shell session for the terminal tool had its cwd inside `/Volumes/Cherry6TB/osti_corpus/` from earlier in the session. `fork()` / `posix_spawn()` resolves the parent's cwd inode as part of process creation. With the volume's catalog locked, every new `bash` / `echo` invocation **blocks at fork-time before exec()**. The wedge "spreads" to seemingly-unrelated paths (`/tmp`, `/Users/stevens`) because the **fork itself** is blocking, not the path the new process would touch.

Spotlight contention can also cascade across volumes: `mds_stores` holds global locks while flushing its store, so `mdutil -s` and even `search_files` over `/Users` can hang on the same indexer thread.

## Direct evidence the bytes are fine

Even mid-wedge, **direct `stat` of a known path returned in 68 ms:**

```
$ time stat /Volumes/Cherry6TB/osti_corpus/_stage_flat/873852.pdf
... rw-r--r-- 2 stevens staff 0 856601 ...
real    0m0.068s
```

`stat` resolves a path directly (no directory walk, no catalog scan). The block layer and the file's inode are healthy. The wedge is **catalog walk / fork-cwd**, not the drive itself.

## DO NOT unplug the drive when this happens

Tempting because the drive looks "dead", but:
- The drive's bytes are intact (proven by `stat`).
- Spotlight has half-built index state in-memory and may have dirty buffers queued for the HFS+ catalog.
- A hard yank during a catalog write **can corrupt the HFS catalog**, taking 99,786 hardlinks + `_state/catalog.sqlite` + `_audit/inventory.sqlite` with it.

Cherry6TB consolidation took days. Don't risk it for a Spotlight bug.

## The actual fix (run from a fresh Terminal.app window — not via the agent)

```bash
# Tell Spotlight to leave Cherry6TB alone, permanently for this volume.
sudo mdutil -i off /Volumes/Cherry6TB
sudo mdutil -E   /Volumes/Cherry6TB    # erase the index — stops the reindex storm cold

# Verify:
ls /Volumes/Cherry6TB/
```

If `mdutil` itself hangs (Spotlight is wedged hard):

```bash
sudo killall -9 mds_stores mdworker_shared mdworker
# launchd will respawn them, but with the volume excluded they'll behave.
```

Equivalent GUI path: **System Settings → Spotlight → Search Privacy → +** and add `/Volumes/Cherry6TB`. Same effect, no sudo needed.

## Why this must be done out-of-band from the agent

The agent's persistent shell session is **already poisoned** by the time the wedge is visible — its cwd is stuck on the locked volume, so no fork inside that session succeeds. Even `sudo mdutil -i off ...` issued via the agent will hang at fork. **The fix has to come from a fresh shell** that has never `cd`'d into the volume.

After the unwedge, the agent's terminal session will need restart (the cwd is still poisoned even after Spotlight is silenced, because the inode pointer in the shell's process state is stale). New session = clean state.

## Permanent prevention

Always disable Spotlight on Cherry6TB after any reconnect or after any large file-add pass (rsync, tar, large promote). One-liner to make part of standard mount procedure:

```bash
mdutil -s /Volumes/Cherry6TB | grep -q "Indexing enabled" && sudo mdutil -i off /Volumes/Cherry6TB
```

This is a write-once setting (persisted in the volume metadata), so once disabled it stays disabled across unmounts and remounts — until someone re-enables it via Settings.

## Diagnostic signals to recognize early (before the cascade)

If you see ANY of these, stop running shell commands against the volume and check Spotlight status immediately (from outside the persistent agent shell if possible):

- `stat <known-path>` works fast but `ls <parent-dir>` hangs → Spotlight catalog lock, not drive
- `lsof /Volumes/Cherry6TB` returns empty (no process holds the drive) but I/O still hangs → kernel-side lock, almost always mds
- `ps | grep mdworker` shows 4+ workers spawned in last 30 min → active reindex
- `mdutil -s <volume>` times out → indexer is wedged

The skill's existing pitfall "DON'T enumerate Cherry6TB root with `ls` / `find -maxdepth 1`" covers the *symptom*. This file covers the *root cause* and the *fix*. Standard procedure: disable Spotlight indexing on Cherry6TB at first mount, treat it as a write-only archive volume from the OS's POV.

## Generalizes to

- Any external HFS+ / APFS volume holding 50K+ small files in flat directories (image archives, document corpora, model snapshot dirs)
- Any volume mounted on a Mac where Spotlight indexing would be wasted effort (data-only archives, no full-text search needs)
- Any agent session running long shell-pipeline work — keep `cwd` somewhere stable like `~` or `/tmp`, not inside a working data volume that may go unresponsive
