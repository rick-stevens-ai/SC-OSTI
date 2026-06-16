# Cross-host OSTI consolidation audit pattern

When Rick asks "give me an update on consolidating PDFs / .MDs to Cherry6TB" — or any
"where do we stand on the cross-host stores" variant — the answer is always a table
showing what's local on Cherry6TB vs what's at each authoritative source, with the gap
as a single number per row. This file captures the recipe.

## The canonical OSTI + parsed-text stores (as of 2026-06-12)

| Store | Type | Authoritative source | Cherry6TB path |
|---|---|---|---|
| `osti_fulltext/` | Legacy PDFs (oldest, 2024-era) | **Cherry6TB IS the source** — not on cels | `/Volumes/Cherry6TB/osti_fulltext/<year>/<year>/<id>.pdf` |
| `osti_fulltext_v2/` | V2 refresh PDFs (2026 expansion) | `cels-rbdgx2:/rbstor/stevens/osti_fulltext_v2/` | `/Volumes/Cherry6TB/osti_fulltext_v2/` |
| `osti_fulltext_unpay/` | Unpaywall fallback PDFs | `cels-rbdgx2:/rbstor/stevens/osti_fulltext_unpay/` | `/Volumes/Cherry6TB/osti_fulltext_unpay/` |
| `osti_fulltext_v2_md/` | Marker OCR output (.md + .json) | `cels-rbdgx2:/rbstor/stevens/osti_fulltext_v2_md/` (kept fresh by `scripts/marker_mirror.sh` daily cron) | `/Volumes/Cherry6TB/osti_fulltext_v2_md/` |
| `Ozan_PARSED_PDFS/` | Nougat (.mmd) OCR output, science-domain partitioned | `cels-rbdgx2:/rbstor/stevens/Ozan/PARSED_PDFS/` (5 subdirs: DNA-Repair-MD, Microbial-Physiology-MD, PDE-EQN-MD, QC-MD, RNA-PDF-MD; 17,491 .mmd files, 1.1G) | `/Volumes/Cherry6TB/Ozan_PARSED_PDFS/` |
| `osti_recovery_2026-06-09/` | Snapshot of a stratified recovery probe (30 PDFs + log) | `cels-rbdgx2:/rbstor/stevens/osti_recovery_2026-06-09/` | `/Volumes/Cherry6TB/osti_recovery_2026-06-09/` |

There are also Aurora/Polaris locations under `/lus/flare/projects/AuroraGPT/stevens/`
that may hold parsed text from independent OCR/extraction runs — see the ALCF auth note
in SKILL.md pitfalls for how to reach them.

**xCards markdown corpora** (`~/Dropbox/ARGONNE-PAPERS/{GOOD,XCARDS}/MARKDOWN-{DATA,MODEL,AGENT}-CARDS`,
~1.2G total, 4,628+1,231+86 .md files each) live in Dropbox and sync to m1 automatically.
They're parsed Argonne-paper artifacts but NOT in the Cherry6TB consolidation path by default.
Ask Rick before mirroring them — they may want Dropbox-only.

## The audit recipe (copy-paste-runnable)

Run from m1. Each side reports `du -sh` + per-extension file counts so a single glance
shows whether Cherry6TB is in sync with each source.

```bash
echo "=== Cherry6TB local ==="
for d in /Volumes/Cherry6TB/osti_fulltext \
         /Volumes/Cherry6TB/osti_fulltext_v2 \
         /Volumes/Cherry6TB/osti_fulltext_unpay \
         /Volumes/Cherry6TB/osti_fulltext_v2_md \
         /Volumes/Cherry6TB/Ozan_PARSED_PDFS \
         /Volumes/Cherry6TB/osti_recovery_2026-06-09; do
  if [ -d "$d" ]; then
    echo "--- $d ---"
    du -sh "$d" 2>/dev/null
    echo -n "  .pdf: "; find "$d" -name "*.pdf" 2>/dev/null | wc -l
    echo -n "  .md:  "; find "$d" -name "*.md"  2>/dev/null | wc -l
    echo -n "  .mmd: "; find "$d" -name "*.mmd" 2>/dev/null | wc -l
    echo -n "  .json:"; find "$d" -name "*.json" 2>/dev/null | wc -l
  fi
done

echo ""
echo "=== cels source enumeration (wide regex — catches non-osti-named dirs) ==="
ssh cels-rbdgx2 'for d in /rbstor/stevens /home/stevens; do
  find "$d" -maxdepth 3 -type d \
    \( -iname "*osti*" -o -iname "*fulltext*" -o -iname "*marker_*" \
       -o -iname "*parsed_pdf*" -o -iname "*PARSED_PDFS*" \
       -o -iname "MARKDOWN-*" -o -iname "*ocr*" -o -iname "*olmocr*" \) 2>/dev/null
done | grep -v -E "(\.git|node_modules|/cache/|venv|envs/marker|pkgs/markdown|sphinx|/\.local/)" | sort -u'

echo ""
echo "=== Per-dir size + counts on cels ==="
ssh cels-rbdgx2 'for d in <list-from-above>; do
  echo "--- $d ---"; du -sh "$d" 2>/dev/null
  echo -n "  .pdf: "; find "$d" -name "*.pdf" 2>/dev/null | wc -l
  echo -n "  .md:  "; find "$d" -name "*.md"  2>/dev/null | wc -l
  echo -n "  .mmd: "; find "$d" -name "*.mmd" 2>/dev/null | wc -l
done'
```

**Don't trust an `osti_*`-only regex** — Nougat OCR output landed under `Ozan/PARSED_PDFS/`
(no "osti" anywhere in the name) and was invisible to the narrower probe. Always widen
to `parsed_pdf*`, `PARSED_PDFS`, `marker_*`, `olmocr*`, and the markdown-card patterns
when sweeping for consolidation. `Ozan/PARSED_PDFS` returned 0 hits on `.pdf|.md|.txt`
but had 17,491 `.mmd` files — extend the extension-count loop too, not just `.pdf`/`.md`.

The `du` is the fast indicator — if size matches within a few percent (APFS block
alignment can shift counts by ~10MB even for identical content), counts will match too.
A 10x size disparity means the pull was never completed.

## Composing the response

Always reply with a single table, one row per store, columns =
(store, local, source, gap). End with **the one outstanding action** if any:

> ### Cherry6TB OSTI consolidation — current state
>
> | Store | Cherry6TB local | Cels source | Gap |
> |---|---|---|---|
> | `osti_fulltext/` (legacy) | 387G / 67,590 PDFs ✓ | — | none — Cherry6TB IS the source |
> | `osti_fulltext_v2/` | 4.5G / 814 PDFs | 208G / 68,471 PDFs | **67,657 PDFs missing (~204G)** |
> | `osti_fulltext_unpay/` | 59G / 24,427 PDFs ✓ | 60G / 24,427 PDFs | complete |
> | `osti_fulltext_v2_md/` (Marker OCR) | 486M / 4,206 .md ✓ | 478M / 4,207 .md | complete |
>
> **Outstanding gap**: 204G v2 PDF pull from cels → Cherry6TB. ~6–8h at this morning's
> rsync throughput. Want me to start it?

The table is what Rick wants — leading with prose burns his attention before the
data. Surface the one actionable gap as a yes/no question at the end.

## Cross-host transfer commands (paste-ready)

Always use Homebrew rsync (`/opt/homebrew/bin/rsync`) — see SKILL.md pitfall about
openrsync. Background via `terminal(background=true, notify_on_complete=true)`:

```bash
# unpay PDFs (~60G / 24K files / ~6h)
/opt/homebrew/bin/rsync -av --info=progress2 --partial \
  cels-rbdgx2:/rbstor/stevens/osti_fulltext_unpay/ \
  /Volumes/Cherry6TB/osti_fulltext_unpay/

# v2 PDFs (~204G / 68K files / overnight)
/opt/homebrew/bin/rsync -av --info=progress2 --partial \
  cels-rbdgx2:/rbstor/stevens/osti_fulltext_v2/ \
  /Volumes/Cherry6TB/osti_fulltext_v2/

# v2 Marker OCR (.md + .json, ~478M / ~8K files / seconds-minutes)
# This is now also handled by the daily cron at scripts/marker_mirror.sh.
/opt/homebrew/bin/rsync -av --info=progress2 --partial \
  cels-rbdgx2:/rbstor/stevens/osti_fulltext_v2_md/ \
  /Volumes/Cherry6TB/osti_fulltext_v2_md/
```

For Aurora/Polaris pulls (parsed text under `/lus/flare/projects/AuroraGPT/stevens/`),
the same rsync runs via the cherryrd ControlMaster socket — but Rick has to do the
initial interactive `ssh polaris` (or aurora) from cherryrd ONCE first to park the
socket. See SKILL.md pitfall about ALCF MFA + ControlMaster. After the socket is hot:

```bash
# Conceptual — exact subpath depends on where the parsed text actually lives.
# Run an enumeration step first (ssh polaris 'ls /lus/flare/projects/AuroraGPT/stevens/')
# while the socket is fresh, then craft the rsync.
ssh cherryrd '/opt/homebrew/bin/rsync -av --info=progress2 --partial \
  polaris:/lus/flare/projects/AuroraGPT/stevens/<subpath>/ \
  /tmp/aurora_parsed_text/'
# Then a second hop m1 ← cherryrd → Cherry6TB.
```

(rsync from m1 directly through `polaris:...` works too if the m1-side ssh control
socket is alive, but the ALCF socket lives on cherryrd, so two-hop is normal.)

## Pitfalls

- **Don't `ls /Volumes/Cherry6TB/` to enumerate stores** — HFS catalog stalls 60s+ on
  the root. Direct path access (`ls /Volumes/Cherry6TB/osti_fulltext/`) works fine.
- **Counts can differ by 1 between `.md` and `.json` in the Marker mirror** — one
  paper has metadata but no markdown output (Marker conversion failure). Not a
  consolidation gap; the source has the same skew.
- **The legacy `osti_fulltext/` Cherry6TB store has the double-`<year>/<year>/<id>.pdf`
  layout** — see SKILL.md pitfall. `osti_fulltext_v2/`, `osti_fulltext_unpay/`, and
  `osti_fulltext_v2_md/` all use flat single-directory layout.
- **Don't kill an in-progress rsync just to switch to a faster rsync version** — the
  old rsync 2.6.9 transfers `-av --partial` correctly, it just lacks `--info=progress2`.
  Killing mid-stream loses partial-file state; let it finish, install brew rsync
  for the next pull.
- **Verify rsync exit-success WITH a source-vs-dest count diff** — the macOS bundled
  openrsync silently exited 0 in 33s after dumping its help page (because
  `--info=progress2` is unknown to it), leaving Cherry6TB with **0 PDFs** out of 24,427.
  The `notify_on_complete` hook fired "exit code 0" and looked successful. The audit
  step (`ssh cels 'find ...' | wc -l` vs `find /Volumes/Cherry6TB/...' | wc -l`) is
  the witness that catches this. Always run the count audit after any background
  bulk rsync, regardless of exit code.
- **`osti_*`-only enumeration regex misses Nougat/parsed-text dirs.** `Ozan/PARSED_PDFS`
  on cels has 17,491 `.mmd` files across 5 science-domain subdirs but doesn't match
  `*osti*`. Widen the regex to include `parsed_pdf*`, `PARSED_PDFS`, `marker_*`,
  `olmocr*`, `MARKDOWN-*`. And extend the file-count loop beyond `.pdf`/`.md` to
  include `.mmd` and `.txt` — `Ozan/PARSED_PDFS` returned 0/0/0 on .pdf/.md/.txt
  but had 17,491 .mmd, looking empty in a too-narrow probe.
- **xCards in Dropbox aren't automatically on Cherry6TB.** The MARKDOWN-{DATA,MODEL,AGENT}-CARDS
  dirs live in `~/Dropbox/ARGONNE-PAPERS/` and sync to m1, but Cherry6TB doesn't
  carry them unless explicitly mirrored. Ask Rick before adding them to the
  consolidation path — they may prefer Dropbox-only.
- **Aurora/Polaris pulls are gated on Rick's CRYPTOCard.** Plan around it: prep the
  enumeration commands, the rsync command, and the dedup logic locally first, hand
  Rick a one-paragraph instruction with the OTP step explicit, then run the actual
  pulls once the socket parks. See vault card `infra/host-reachability-map.md` for
  the full ALCF ControlMaster pattern (one interactive ssh from cherryrd parks a
  socket valid for ~24h; subsequent rsync rides through without re-prompting).

## Dedup-aware multi-source rsync pattern

When pulling N sources to Cherry6TB and some files may already be local (e.g. the
prior pull staged 814 of 68,471 PDFs already), use `--ignore-existing` to skip the
already-present files without touching them. For genuinely new dirs (Ozan/PARSED_PDFS),
plain `-a` is fine. Run sequentially in one background process with `notify_on_complete`
so a single notification covers the whole sweep:

```bash
RSYNC=/opt/homebrew/bin/rsync

# [1/N] biggest first — gives the earliest signal on transfer health
$RSYNC -a --ignore-existing --partial --info=stats2 \
  cels-rbdgx2:/rbstor/stevens/osti_fulltext_v2/ \
  /Volumes/Cherry6TB/osti_fulltext_v2/

# [2/N] novel dir — no dedup needed
$RSYNC -a --partial --info=stats2 \
  cels-rbdgx2:/rbstor/stevens/Ozan/PARSED_PDFS/ \
  /Volumes/Cherry6TB/Ozan_PARSED_PDFS/

# ... more sources ...

# Final verification — counts on both sides per store
for d in <Cherry6TB dirs>; do
  size=$(du -sh "$d" 2>/dev/null | cut -f1)
  pdfs=$(find "$d" -name "*.pdf" 2>/dev/null | wc -l)
  mds=$(find "$d" -name "*.md" 2>/dev/null | wc -l)
  mmds=$(find "$d" -name "*.mmd" 2>/dev/null | wc -l)
  echo "  $size  pdf=$pdfs  md=$mds  mmd=$mmds  $d"
done
```

Note the **biggest first** ordering: a 208G transfer surfaces network/disk problems
earlier than a 96K probe dir. If the big one fails, you haven't wasted hours on the
small ones first.

For sources where the same OSTI ID might be re-OCR'd with a different/newer Marker
run (collision on `<id>.md` filename), `--ignore-existing` is wrong — it'll keep the
stale local copy. Use `--update` (only replace if source is newer) or land in a
holding dir and content-hash-diff before merging.

## Fleet sweep — knowing which hosts to enumerate

Before sweeping for parsed-text dirs across hosts, consult vault card
`~/Dropbox/XFER/memory-vault/infra/host-reachability-map.md` for which hosts you can
reach without prompting. As of 2026-06-12, KEY-reachable hosts that might hold
parsed-text output:

- `cels-rbdgx1/2/3` (shared `/rbstor` — enumerate any ONE of them)
- `uicgpu` (`/home/stevens/Dropbox/XFER/lucid_marker_queue_results` — usually empty
  unless a Marker run is mid-flight; the actual worker stages are under `/data/stevens/`)
- `cherryrd` (mostly forwards to other hosts; check `~/Dropbox/ARGONNE-PAPERS/`)
- `alcf-sophia` (rare; not historically a Marker host)

AUTH-gated hosts that need MFA before enumeration:
- `aurora`, `polaris`, `crux` — `/lus/flare/projects/AuroraGPT/stevens/` is the canonical
  Aurora staging area; Polaris equivalent under `/eagle/projects/AuroraGPT/`. Ask Rick
  for the one-shot OTP before designing the rsync.

DOWN as of last audit:
- `cels-hcdgx2` (open CELS ticket), `m3acbook` (sleeping).

## Related references

- `scripts/marker_mirror.sh` — daily cron that handles the `osti_fulltext_v2_md/`
  side without any intervention.
- `scripts/osti_corpus_status.sh` — broader status snapshot across all seven OSTI
  pipelines (contacts.db, PDF store, xCards, classifier, candidates, recon, OCR,
  live processes). Run this first if Rick's question is vaguer than just "store
  consolidation."
