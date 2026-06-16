#!/usr/bin/env bash
# inventory_corpus.sh — pre-flight inventory for a multi-dir corpus
# before designing any throughput/packing/walltime proposal.
#
# Default shape: OSTI on Cherry6TB. Adapt VOLUME, PATTERN, ID_RE for other corpora.
#
# Output sections:
#   1. Sibling staging dirs found on the volume
#   2. Per-dir total file count + size
#   3. Per-year coverage matrix (rows = dir, cols = year)
#   4. Mixed flat-vs-nested layout warnings
#   5. In-flight rsync/fetch processes that may still be writing
#   6. Cross-dir basename overlap (potential duplicates by stable ID)
#
# Usage:
#   ./inventory_corpus.sh                              # OSTI defaults
#   VOLUME=/data PATTERN='*.json' DIRGREP=papers ./inventory_corpus.sh
#
# Re-runnable, safe, read-only.

set -u

VOLUME="${VOLUME:-/Volumes/Cherry6TB}"
PATTERN="${PATTERN:-*.pdf}"
DIRGREP="${DIRGREP:-osti}"
YEAR_LO="${YEAR_LO:-2006}"
YEAR_HI="${YEAR_HI:-2026}"

echo "=== corpus inventory @ $(date -u +%FT%TZ) ==="
echo "volume=$VOLUME  pattern=$PATTERN  dir-grep=$DIRGREP"
echo

# ---- 1. Sibling staging dirs ----
echo "--- 1. Sibling staging dirs matching /$DIRGREP/i ---"
dirs=()
while IFS= read -r d; do
    [ -d "$VOLUME/$d" ] && dirs+=("$d")
done < <(ls "$VOLUME" 2>/dev/null | grep -iE "$DIRGREP")
if [ "${#dirs[@]}" -eq 0 ]; then
    echo "  (no matching dirs found — adjust DIRGREP)"
    exit 1
fi
printf '  %s\n' "${dirs[@]}"
echo

# ---- 2. Per-dir totals ----
echo "--- 2. Per-dir totals ---"
printf "  %-40s %10s %10s\n" DIR FILES SIZE
for d in "${dirs[@]}"; do
    n=$(find "$VOLUME/$d" -name "$PATTERN" -type f 2>/dev/null | wc -l | tr -d ' ')
    sz=$(du -sh "$VOLUME/$d" 2>/dev/null | cut -f1)
    printf "  %-40s %10s %10s\n" "$d" "$n" "$sz"
done
echo

# ---- 3. Per-year coverage matrix ----
echo "--- 3. Per-year coverage matrix (zero years suppressed) ---"
for d in "${dirs[@]}"; do
    rowparts=()
    for y in $(seq "$YEAR_LO" "$YEAR_HI"); do
        if [ -d "$VOLUME/$d/$y" ]; then
            n=$(find "$VOLUME/$d/$y" -name "$PATTERN" -type f 2>/dev/null | wc -l | tr -d ' ')
            [ "$n" -gt 0 ] && rowparts+=("$y=$n")
        fi
    done
    if [ "${#rowparts[@]}" -gt 0 ]; then
        echo "  $d:"
        for p in "${rowparts[@]}"; do printf "    %s\n" "$p"; done
    fi
done
echo

# ---- 4. Mixed flat-vs-nested layout warnings ----
echo "--- 4. Mixed flat-vs-nested layout warnings ---"
mixed_found=0
for d in "${dirs[@]}"; do
    for y in $(seq "$YEAR_LO" "$YEAR_HI"); do
        ydir="$VOLUME/$d/$y"
        [ -d "$ydir" ] || continue
        flat=$(find "$ydir" -maxdepth 1 -name "$PATTERN" -type f 2>/dev/null | wc -l | tr -d ' ')
        nested=$(find "$ydir" -mindepth 2 -maxdepth 3 -name "$PATTERN" -type f 2>/dev/null | wc -l | tr -d ' ')
        if [ "$flat" -gt 0 ] && [ "$nested" -gt 0 ]; then
            echo "  MIXED $d/$y  flat=$flat  nested=$nested  (probable duplicates — verify byte-identity)"
            mixed_found=1
        fi
    done
done
[ "$mixed_found" -eq 0 ] && echo "  (none — all dirs are layout-consistent)"
echo

# ---- 5. In-flight processes ----
echo "--- 5. In-flight rsync/fetch processes (still writing?) ---"
pgrep -af "rsync|fetch|$DIRGREP" 2>/dev/null | grep -v "$0" | grep -v grep | head -20 || echo "  (none)"
echo

# ---- 6. Cross-dir basename overlap ----
echo "--- 6. Cross-dir basename overlap (top 20) ---"
tmp=$(mktemp)
for d in "${dirs[@]}"; do
    find "$VOLUME/$d" -name "$PATTERN" -type f 2>/dev/null |
        awk -F/ -v dir="$d" '{print $NF, dir}'
done > "$tmp"
# basenames appearing in >=2 different dirs
awk '{names[$1]=names[$1]" "$2} END {for (n in names) {split(names[n], a, " "); seen=""; uniq=0; for (i in a) if (a[i] != "" && index(seen, " "a[i]" ") == 0) {seen=seen" "a[i]" "; uniq++} if (uniq >= 2) print uniq, n, seen}}' "$tmp" |
    sort -rn | head -20
rm -f "$tmp"
echo
echo "=== inventory done ==="
