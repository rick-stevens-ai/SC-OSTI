# Keeping high-churn git repos out of Dropbox

## The problem

Dropbox's sync engine sees `.git/objects/`, `.git/refs/`, `.git/index`, and the working tree all changing simultaneously during normal git operations (commit, rebase, checkout, push). It can't reconcile rapid concurrent edits from multiple replicas (or from a single replica plus a hung subprocess) and its conflict-resolver picks a "winner" — silently rolling back the losers. The losers can be files, branches, commits, or reflog entries.

This is not a Dropbox bug per se — it's an inherent mismatch between Dropbox's whole-file sync model and git's "many small files change atomically as a unit" model.

## When this matters

- Repos under heavy concurrent agent activity (Kukla on m1 + Ollie on cherryrd both touching the same `~/Dropbox/<repo>/`)
- Repos where rapid sequential `commit`/`push`/`rebase` operations happen
- Any time a git subprocess hangs while holding partial state (the credential-helper hang on m1 directly triggered the 2026-06-08 wipe)
- Anything where losing a commit is more painful than losing Dropbox-sharing

## The pattern

**Work in a non-Dropbox path. Sync the final state (or use git remotes) to share.**

```
~/.hermes/work/<repo>/        # primary working tree, NOT synced
~/code/<repo>/                # alternative — also fine
```

Then:

- **For sharing with a peer agent on another host:** push to a git remote (GitHub, gitea, even a bare remote on the peer's home dir). DON'T rely on Dropbox to ferry git state between machines.
- **For sharing artifacts (built binaries, generated reports) with a peer:** copy the FINAL output into `~/Dropbox/XFER/<task>/`, not the working tree.
- **For collaboration on the repo source itself:** use git's actual collaboration tools (push + pull from a real remote). Dropbox is not a substitute for git's distributed model — using it as one creates exactly the conflict surface that destroyed the multimodal-indexing branch.

## When Dropbox-backed git is OK

- One-machine, single-agent work where Dropbox is just a convenient backup
- Documentation repos (mostly markdown, low churn, no rebase/squash)
- Reference checkouts where nobody commits

## Migration recipe (existing Dropbox repo → safe location)

```bash
# 1. Make sure remote is up to date (push everything that matters)
cd ~/Dropbox/<repo>
git push origin --all
git push origin --tags

# 2. Clone fresh into safe location
git clone <remote_url> ~/.hermes/work/<repo>

# 3. Verify the new clone has everything
cd ~/.hermes/work/<repo>
git log --all --oneline | wc -l    # should match old clone

# 4. Stop using the Dropbox path; optionally rename it
mv ~/Dropbox/<repo> ~/Dropbox/.archived-<repo>-$(date +%Y%m%d)
```

## When the work IS already in Dropbox and the wipe just happened

Don't migrate before recovering. See main SKILL.md Paths 1-3.
