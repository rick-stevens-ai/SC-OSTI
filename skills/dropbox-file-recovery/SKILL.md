---
name: dropbox-file-recovery
description: "Recover lost/0-byte/wiped files in a Dropbox-synced directory before declaring data loss. Covers the content-addressed cache, cross-replica recovery via Tailscale-reachable Dropbox peers, and the sync-conflict race that wipes files (e.g. during rapid git operations on a Dropbox-backed repo)."
version: 1.0.0
author: Kukla
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [Dropbox, Data-Recovery, Filesystem, Sync, Git, Forensics]
    related_skills: [github-pr-workflow, kukla-ollie-mailbox]
---

# Dropbox File Recovery

For when files in a Dropbox-synced directory have disappeared, gone to 0 bytes, or otherwise look wrong. **Before declaring data loss, run the checks in this skill — recovery is usually possible in <10 minutes.**

## When this skill applies

- "I had files at `~/Dropbox/<path>/X.py` yesterday, they're gone today"
- A file opens but is 0 bytes locally even though it should have content (common with `~/Dropbox/DOE-LHP-DARIO/*.docx`, lighthouse archives, Genesis docs on Rick's m1-mac-mini)
- After a burst of file activity in a Dropbox dir (especially `git checkout -b`, `git rebase`, rapid commits, archive extraction), files vanish or revert
- `git log` shows commits missing that you remember making; `git reflog` is also short
- A whole subdirectory's contents disappeared from disk but you didn't `rm` anything
- `find <dir> -type f -size 0` returns a list of files that shouldn't be empty

## Three recovery paths, in order of effort

### Path 1 — Content-addressed cache (fastest, ~5 min)

Dropbox keeps every file version it has ever synced in a local content-addressed cache, even after the file disappears from your working tree. Files are stored by content hash (NOT by original path), so you grep their contents to find them.

**Location (macOS):** `~/Dropbox/.dropbox.cache/old_files/`
**Location (Linux):** `~/Dropbox/.dropbox.cache/old_files/` (same)

```bash
# Verify the cache exists and has recent entries
ls ~/Dropbox/.dropbox.cache/old_files/ | head -5
ls ~/Dropbox/.dropbox.cache/old_files/ | wc -l   # On Rick's m1: ~21,000 entries
```

**Recovery procedure** — see `scripts/recover_from_cache.py` for the full pattern. The skeleton is:

1. Enumerate cache files in a plausible size range for the lost files
2. Open each in binary mode, grep contents for distinctive strings (class names, distinctive docstrings, function names, project-specific identifiers — NOT generic words)
3. Build a `cache_hash → intended_path` map by content fingerprint
4. `shutil.copy2()` each match into the correct path under `~/Dropbox/<repo>/`

**Critical pitfall — DON'T do this:**

```bash
# THIS TIMES OUT every time. ~21K files × shell glob expansion × grep startup = 60s+
cd ~/Dropbox/.dropbox.cache/old_files && grep -l "MultiSearch" *
```

**DO this instead** — Python with explicit size pre-filter, then read in binary:

```python
# See scripts/recover_from_cache.py for full version
import os
CACHE = os.path.expanduser("~/Dropbox/.dropbox.cache/old_files")
NEEDLES = [b"MultiSearch", b"IndexBuilder", b"search_hybrid"]  # distinctive strings
for fname in os.listdir(CACHE):
    fp = os.path.join(CACHE, fname)
    try:
        sz = os.path.getsize(fp)
        if not (500 <= sz <= 50_000):  # narrow by file size first
            continue
        with open(fp, "rb") as f:
            data = f.read()
        for needle in NEEDLES:
            if needle in data:
                print(f"HIT: {fp} ({sz}b) — {needle.decode()}")
                break
    except OSError:
        pass
```

### Path 2 — Cross-replica recovery (5-15 min)

If the cache doesn't have it (file too old, never synced from your machine, or cache was cleared), check OTHER Dropbox replicas. On Rick's fleet, m3acbook-pro (Tailscale `<tailnet-m3acbook>`) is consistently the most-current replica when m1-mac-mini shows corruption — the corruption is m1-specific.

```bash
# Check the same path on another host
timeout 15 ssh stevens@<tailnet-m3acbook> "ls -la ~/Dropbox/<path>/ 2>&1 | head -20"

# If files are healthy there, rsync them back
rsync -av stevens@<tailnet-m3acbook>:~/Dropbox/<path>/ ~/Dropbox/<path>/

# For a git repo, also fetch the .git directory itself (commits/branches that vanished)
rsync -av stevens@<tailnet-m3acbook>:~/Dropbox/<repo>/.git/ ~/Dropbox/<repo>/.git/
```

**Don't rsync `.git/` while you're in the middle of a git operation** — wait for any in-progress process to die first (`pgrep -af git`), then rsync.

**macOS bundled `rsync` is ancient and silently fails on modern flags.** `/usr/bin/rsync` on macOS is openrsync 2.6.9-compat (last upstream rsync 2.6.9 was 2006). It does NOT support common modern flags like `--info=progress2`, `--mkpath`, `--chmod=...`, several `--exclude-from` variants, and will print its full help-text dump and exit with code 0 — looking superficially like success — when given any unsupported flag. **Symptom:** rsync command "completes" in <60s for a multi-GB transfer with 0 files actually copied; check verification (`du -sh dest && find dest -type f | wc -l`) and you'll see empty dest.

**Fix once per host:** `brew install rsync` → modern rsync at `/opt/homebrew/bin/rsync` (currently 3.4.4, protocol 32). Always invoke by full path for cross-host pulls:

```bash
/opt/homebrew/bin/rsync -av --info=progress2 --partial \
  cels-rbdgx2:/path/to/source/ \
  /local/dest/
```

**Fallback if you can't install:** stick to the 2.6.9-compatible flag subset — `-av --partial` works fine, just no progress display. Don't pass `--info=*`, `--mkpath`, or any flag introduced post-2006.

**Why not just rely on PATH:** brew installs to `/opt/homebrew/bin/rsync` but `/usr/bin/rsync` is earlier in default PATH, so `which rsync` and bare `rsync` both still hit the broken one. Brew warns about this on install ("rsync is shadowed by /usr/bin/rsync"). Use the full path explicitly in scripts.

**Pre-flight: is this a known-corruption directory?** Per memory, the following on m1-mac-mini are routinely 0-byte locally but live on m3acbook-pro:

- `~/Dropbox/DOE-LHP-DARIO/` (~147 files affected, ~2.5MB total)
- `~/Dropbox/DOE_AI_Lighthouse_Challenges.pdf`
- Various `~/Dropbox/GENESIS-RFA/` PDFs

If you're asking the user "where did file X go" and X is in one of these subtrees, check m3acbook-pro first — don't waste time on Path 1.

### Path 3 — Dropbox web/API recovery (last resort)

If neither path 1 nor path 2 has the file, Dropbox.com keeps file history server-side. Two sub-paths:

- **Web UI:** dropbox.com → file → "Version history" — up to 30 days for free, 180+ days for Plus/Pro
- **Deleted files:** dropbox.com → "Deleted files" — restorable via UI

This requires user interaction (no headless API for version restore in a useful form for our case) — surface a clear "I can't recover from local sources, here's the dropbox.com URL to check" message rather than spinning.

## The sync-conflict race that destroys git work

**Observed 2026-06-08:** during a sequence of `git commit` + `git push` (hang) + `git commit` + `git push` (hang) operations on a Dropbox-synced repo at `~/Dropbox/OLLIE/ump-memory/`, the Dropbox client's conflict-resolver wiped:

- 16 multimodal-indexing source files
- 3 git commits
- The branch ref `refs/heads/feature/multimodal-indexing`
- The reflog entries for those commits

…from disk on m1-mac-mini. All three Dropbox replicas (m1, m3acbook-pro, cherryrd via Dropbox) converged on the pre-feature state. Recovery was via Path 1 (cache).

**Why it happened:** Dropbox sees rapid concurrent changes to `.git/refs/`, `.git/objects/`, working tree, and `.git/index`, can't reconcile them with simultaneous edits on other replicas, picks a "winner" snapshot, and silently rolls the others back. The hung credential-helper processes kept the working tree in a half-changed state long enough for the resolver to fire.

**Mitigation rule** — do NOT do rapid git operations on a Dropbox-synced repo while a credential helper or any other git subprocess is hung. If `git push` hangs, kill it FULLY (helper subprocess too) before doing anything else:

```bash
# Full kill of any wedged git op + helpers
pkill -f "git-credential-osxkeychain"
pkill -f "git push"
sleep 2
pgrep -af "credential-osxkeychain|git push|git fetch" | grep -v grep
# Empty output = safe to proceed
```

**Better mitigation:** for repos under heavy concurrent agent activity, work in a non-Dropbox path (`~/code/<repo>/` or `~/.hermes/work/<repo>/`) and only sync the final state to Dropbox if it needs sharing. See `references/non-dropbox-git-work-pattern.md` for the pattern.

## The FF-merge sync-replay loss (distinct from rapid-ops race)

**Observed 2026-06-09:** Kukla had a local commit on `fix/markdown-backend-and-get-route` in `~/Dropbox/OLLIE/ump-memory/`, NEVER pushed to origin (push hung repeatedly on osxkeychain). Sibling agent Ollie, on a different host with write access to the same Dropbox repo, separately worked on `main`, fast-forward-merged a feature, pushed to origin. When m1's git client next ran (or Dropbox sync replayed Ollie's `.git/` state onto m1), the FF-merge replaced m1's working tree + `HEAD` was force-moved to Ollie's tip. **The un-pushed branch ref AND its commit were silently dropped** from `.git/refs/heads/`, AND from the reflog, AND from `.git/objects/` (orphaned objects gc'd by the sync diff). `git cat-file -p <sha>` returned `fatal: Not a valid object name`.

This is NOT the rapid-ops race (no hung helpers, no concurrent commits — the loss happened cleanly via sync replay). The fingerprint is different:

| | Rapid-ops race | FF-merge sync-replay |
|---|---|---|
| Trigger | Hung credential helper + rapid commits on one host | Another host pushes; sync replays the new `.git/` state |
| Reflog | Short but present | Empty for the lost commits |
| Working tree | Half-changed mid-loss | Cleanly replaced with the remote tip |
| Symptom | Files vanish from working tree | `git status` says "up to date with origin/main" but your branch is gone |

**Prevention rule (load-bearing):** in any Dropbox-synced git repo where a sibling agent has write access, **push every local branch to origin immediately** — do not leave un-pushed work overnight, do not leave un-pushed work while another agent is active. If push hangs (osxkeychain, network), either fix the auth path FIRST or move the work to a non-Dropbox path (Path: `~/code/<repo>/`) before continuing.

**Recovery is via Path 1 (content cache).** The skeleton from `scripts/recover_from_cache.py` works as-is — narrow by mtime window (last 24h) AND size band (1KB-50KB for source files) AND distinctive identifiers (class names, module docstrings, unique function names). On this M1 the full recovery of 4 source files took ~5 minutes from "git cat-file says no" to "tests pass on recovered files." See `references/ff-merge-sync-replay-recovery.md` for the worked example.

**Post-recovery, commit to a FRESH branch and push immediately.** Do NOT reuse the original branch name without first verifying origin doesn't have a stale ref under that name.

**Post-recovery, MIGRATE the repo out of Dropbox BEFORE re-committing — not after.** This rule is load-bearing and was learned the hard way on 2026-06-09: Kukla recovered 4 source files from the cache, committed to a fresh branch in the SAME Dropbox repo, push 403'd (token scope), kept working on other files in the recovered repo while waiting for the peer to push, and within the hour the second commit + branch were wiped by the same FF-merge sync-replay race. The first wipe and second wipe were ~60 minutes apart, same repo, same agent, same root cause. The migration recipe in `references/non-dropbox-git-work-pattern.md` should run BETWEEN "files restored to working tree" and `git add` — not as a follow-up after the fresh commit is in place. If you can't push immediately (token scope, network, peer offline), `git clone` the Dropbox repo to `~/code/<repo>/` FIRST, do the commit there, and use `git bundle` for handoff if push stays blocked. The Dropbox copy is read-only from the moment recovery completes until origin has caught up.

## Stub-only files: cache and cross-replica both fail

**Critical distinction** (learned 2026-06-13 on `~/Dropbox/ARGONNE-PAPERS/GOOD/ALL-PAPERS/`, 18,975 PDF entries):

A file that was NEVER materialized on this host — i.e. it has only ever existed as a Dropbox online-only stub — will be **absent from BOTH the local content cache AND from peer hosts that also only ever held the stub**. The recovery paths in this skill assume the file was materialized at least once somewhere; if it never was, you need a different strategy entirely (re-derive from source, request materialization, or accept loss).

**Fast diagnostic — was this file ever materialized anywhere?**

```bash
# Pre-flight 1: does the local cache hold ANY file of this type?
# (Sample magic bytes across the WHOLE cache, not first N — magic byte distribution
# tells you instantly whether files of this class were ever cached locally.)
python3 -c "
import os, pathlib
cache = pathlib.Path.home() / 'Dropbox/.dropbox.cache/old_files'
magic_counts = {}
for f in cache.iterdir():
    if not f.is_file(): continue
    try:
        with open(f, 'rb') as fh: head = fh.read(4)
    except OSError: continue
    magic_counts[head[:4]] = magic_counts.get(head[:4], 0) + 1
# Sort, print top 10
for m, n in sorted(magic_counts.items(), key=lambda x: -x[1])[:10]:
    print(f'{m!r}: {n}')
"
# Look for b'%PDF' (PDFs), b'PK\\x03\\x04' (zips/docx/xlsx), b'\\x89PNG', etc.
# Zero entries with the expected magic = never materialized locally.
```

```bash
# Pre-flight 2: peer host might ALSO have only stubs. Don't trust ls count — check size.
ssh peer-host 'p=~/Dropbox/<path>; echo "ls: $(ls $p | wc -l)"; \
  echo "find -size +1c: $(find $p -type f -size +1c | wc -l)"; \
  echo "sample size: $(stat -c %s "$p/$(ls $p | head -1)" 2>/dev/null || stat -f %z "$p/$(ls $p | head -1)") bytes"'
```

**Stub-marker file sizes are tiny and consistent.** On Rick's fleet, current Dropbox stub size is **18 bytes** (varies by Dropbox client version — typical range 0-30 bytes). If `ls` shows N entries and `find -type f -size +1c` returns 0 OR a sampled file is <100 bytes, the directory is stub-only on that host.

**When BOTH the cache shows zero of this file-class AND the peer shows stubs of the same byte-size**, stop checking other hosts unless you have a known-asleep replica that historically held the materialized form. Two converged stub states is strong evidence the corpus was never materialized.

**Recovery-mode escalation when stub-only is confirmed:**
1. Check `m3acbook` (or whatever the historically-most-current replica is) if it's offline now — request user wake it
2. Look for the same content under a DIFFERENT key system already on disk — e.g. ARGONNE-PAPERS PDFs by paper title likely have heavy OSTI-ID overlap with `osti_fulltext/<id>.pdf` corpora; do an intersection check before treating the corpus as lost
3. Re-derive from source (re-fetch from the publisher, re-extract from upstream archive) — often faster than waiting on a sleeping Mac

## Pitfalls

- **Cache uses content hashes, not paths.** You CANNOT `ls` for "the lost file" — you have to fingerprint contents. Build a unique-string list (function names, distinctive docstrings, project-specific tokens) before scanning.
- **Cache only holds previously-materialized files.** If the file was always a stub on this host, the cache will not have it. Run the magic-byte pre-flight before scanning for needles — saves you from spending time on Path 1 when it can't work.
- **Peer replicas can ALSO be stub-only.** Don't assume "different Mac = real bytes" — `ls | wc -l` matches across stubbed replicas because Dropbox propagates the stub itself. Always verify size on the peer (`find -type f -size +1c` count, or stat a sample file).
- **Shell `grep -l` over 20K+ cache files hangs.** Always use Python with size pre-filter.
- **`find -size 0` on a Dropbox dir is the canonical pre-flight check** for 0-byte sync corruption. Run it before processing files in any `~/Dropbox/<path>/` you don't fully trust: `find ~/Dropbox/<path> -type f -size 0 | head -20`
- **Don't rsync `.git/` while git is running.** Race condition will corrupt the destination too.
- **m1-mac-mini specifically has chronic 0-byte sync corruption in certain subtrees** — see the list above. For those, jump straight to Path 2.
- **`~/.hermes/config.yaml` is a protected file** (write-tool guard). If you need to update Hermes config during recovery, see the `hermes-agent` skill — don't try to patch directly.
- **Time pressure makes Path 3 tempting; resist it.** Path 1 + Path 2 cover ~95% of real-world losses on this fleet and take <10 min combined. Going to dropbox.com always involves user interaction and ~30+ min total.

## What NOT to capture in memory after a recovery

After a successful recovery, capture the recovery PROCEDURE in this skill (which you're reading) and the SPECIFIC FILE LOCATIONS / cache layout if new (memory). Do NOT capture:

- The specific filenames lost in this session ("Files X.py, Y.py were lost on 2026-06-08") — that's task narrative
- Specific cache hashes that worked once ("hash 918ead… is the build_index file") — won't be the same hash next time
- Claims that "Dropbox always corrupts X" — the corruption pattern shifts as the user's setup changes

The durable knowledge is: cache exists, has these properties, recover with this Python pattern, cross-replica fallback is here.

## Supporting files

- `scripts/recover_from_cache.py` — runnable Python skeleton for cache-based recovery (parameterize the needle list and output map)
- `references/non-dropbox-git-work-pattern.md` — pattern for keeping high-churn repos out of Dropbox
- `references/ff-merge-sync-replay-recovery.md` — worked example of recovering un-pushed branch wiped by a sibling agent's FF-merge sync
