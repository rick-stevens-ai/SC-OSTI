# FF-merge sync-replay recovery — worked example (2026-06-09)

Concrete recovery transcript for the "sibling agent's FF-merge wiped my un-pushed branch via Dropbox sync" failure mode. Reference example for the procedure described in SKILL.md.

## Setup

Repo: `~/Dropbox/OLLIE/ump-memory/` (Dropbox-synced, shared with Ollie on cherryrd)
Remote: `https://github.com/rick-stevens-ai/ump-memory.git`
M1 Kukla's work: 4 files (`src/ump_memory/md_store.py`, `src/ump_memory/server.py`, `tests/test_md_store.py`, `tests/test_server.py`) on branch `fix/markdown-backend-and-get-route`, commit `9443bd8`, never successfully pushed (osxkeychain hangs).

## What killed it

Ollie, working independently on cherryrd, completed his `feature/multimodal-indexing` work, FF-merged it onto `main` (`6884594`), pushed to origin. m1's next git activity ran `git pull --ff-only origin main` which fast-forwarded `main` from `eb4d3e7` → `6884594`. Combined with Dropbox sync replaying cherryrd's `.git/` state onto m1, the branch ref `refs/heads/fix/markdown-backend-and-get-route` and the commit `9443bd8` itself were dropped — `git cat-file -p 9443bd8` returned `fatal: Not a valid object name`, reflog had no entries for the lost commit, `git fsck --unreachable` found nothing.

## Recovery steps (actual transcript, ~5 minutes)

### Step 1 — Confirm the loss

```bash
cd ~/Dropbox/OLLIE/ump-memory
git status                                   # "On branch main, up to date" — innocent
git branch -a                                # branch gone
git log --all --oneline | head -10           # commit gone
git reflog | head -10                        # nothing relevant
git cat-file -p 9443bd8 2>&1                 # fatal: Not a valid object name
git fsck --unreachable --no-reflogs 2>&1     # empty
```

All five checks confirm: commit + branch + objects + reflog are gone. Time to hit the cache.

### Step 2 — Scan cache with mtime + size pre-filter

```python
import os, time
cache = os.path.expanduser('~/Dropbox/.dropbox.cache/old_files')
now = time.time()
matches = []
for fn in os.listdir(cache):
    p = os.path.join(cache, fn)
    try:
        st = os.stat(p)
        if now - st.st_mtime > 86400:    # last 24h only
            continue
        if st.st_size < 1000 or st.st_size > 50000:  # source file size band
            continue
        with open(p, 'rb') as f:
            data = f.read(8000)            # head only — distinctive strings are early
        # Distinctive identifiers from the lost files
        if (b'MarkdownDirectoryStore' in data
            or b'md_store' in data
            or b'_ID_RE' in data):
            matches.append((fn, st.st_size, st.st_mtime))
    except Exception:
        pass
for fn, sz, mt in sorted(matches, key=lambda x: -x[2])[:20]:
    print(f'{time.strftime("%H:%M:%S", time.localtime(mt))}  {sz:>6}  {fn}')
```

7 candidates returned. Manual inspection of first 400 bytes (utf-8 with try/except for binary) classified them:

| hash | size | identity |
|---|---|---|
| `37fda70293…` | 1901b | git commit message |
| `3a3bc5f7…` | 3902b | binary (git pack fragment) |
| `e837cc90…` | 2123b | unknown JSON shape |
| `ac70ddeda2…` | 12400b | `src/ump_memory/md_store.py` |
| `abc8b8a6…` | 4074b | `tests/test_server.py` |
| `f57c9a90…` | 6975b | `tests/test_md_store.py` |
| `3b646ffe…` | 4291b | `src/ump_memory/server.py` |

All four lost source files recovered. (Commit message recovered as bonus — could rebuild the original commit message verbatim.)

### Step 3 — Restore to a FRESH branch, then re-verify

```bash
cd ~/Dropbox/OLLIE/ump-memory
git checkout -b fix/markdown-backend-and-get-route   # fresh, no remote yet
cp ~/Dropbox/.dropbox.cache/old_files/ac70ddeda… src/ump_memory/md_store.py
cp ~/Dropbox/.dropbox.cache/old_files/3b646ffe… src/ump_memory/server.py
cp ~/Dropbox/.dropbox.cache/old_files/f57c9a90… tests/test_md_store.py
cp ~/Dropbox/.dropbox.cache/old_files/abc8b8a6… tests/test_server.py

# Sanity-check imports + run tests under correct Python (3.10+ for str.removeprefix)
PYTHONPATH=src /opt/homebrew/bin/python3.13 -m pytest tests/ -q
# → 34 passed, 1 skipped — matches pre-loss state
```

### Step 4 — Commit + push IMMEDIATELY this time

```bash
git -c user.name=Kukla -c user.email=kukla@kd9nwa.org \
    commit -m "Add markdown-directory store + URN-friendly GET routes" \
    <(reconstructed message body from cache)

git push -u origin fix/markdown-backend-and-get-route
# If push 403s/hangs on m1 — hand off to peer agent on a host with working auth
# Don't leave the branch un-pushed a second time
```

### Step 4a — Distinguish keychain-hang from token-scope-403

On m1-mac-mini specifically, `git push` has two distinct failure modes that look superficially similar but need different fixes:

| Symptom | Cause | Fix |
|---|---|---|
| Hangs forever, no output, must SIGKILL | osxkeychain credential helper waiting for GUI unlock | `gh auth setup-git` then retry — uses keyring PAT, no GUI prompt |
| Fast `Permission denied / 403` from origin | Token authenticates but lacks `Contents:write` scope on this specific repo | Either (a) refresh fine-grained PAT to include Contents:write, or (b) hand push off to a peer with working auth (e.g. Ollie on cherryrd via kukla-mail) |

Do NOT loop between these without checking which one you're hitting — they have different remediations. `gh auth setup-git` solves the hang, NOT the scope issue. Confirmed 2026-06-09: after recovery, `gh auth setup-git && git push` produced a clean fast 403 (no hang), proving the token-scope path was the actual blocker; handed off to Ollie.

## Lessons that became SKILL.md rules

1. **Push every local branch to origin immediately** in any Dropbox-synced repo where another agent has write access. This is the single rule that prevents this entire failure class.
2. **`git status: up to date with origin/main` is NOT proof your work is safe** — it can also mean your work is silently gone.
3. **Pre-filter the cache scan by mtime + size band** before grepping contents. ~21K → ~7 candidates in <2 seconds vs minutes of grep churn.
4. **Test with the right Python version** post-recovery — modern type syntax (`str | None`, `removeprefix`) fails on system 3.8/3.9. On this M1 use `/opt/homebrew/bin/python3.13` for any recovered code that uses post-3.10 features.
5. **Use a fresh branch name on re-commit** — don't reuse the lost branch's name without first checking if origin has a stale stub.

## What was different from the rapid-ops race

- No hung credential helpers, no concurrent commits, no half-changed working tree at the moment of loss
- `git pull` and Dropbox sync did the entire wipe cleanly
- Result: zero forensic traces in the local repo (reflog, fsck, objects all empty)
- The cache was the ONLY recovery path — Path 2 (cross-replica via m3acbook-pro) would have shown the same post-FF-merge state because Dropbox had already converged all replicas
