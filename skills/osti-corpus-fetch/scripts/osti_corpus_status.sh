#!/bin/bash
# osti_corpus_status.sh — full-corpus status snapshot across all pipelines.
#
# Run this any time Rick asks "where do we stand on the OSTI refresh /
# corpus / extraction / OCR / classifier / replication-candidates" without
# specifying which layer he means. The answer almost always requires data
# from multiple pipelines and this script collects all of them in one pass.
#
# Covers seven layers:
#   A. contacts.db — paper metadata + DOI coverage + per-year breakdown
#   B. PDF fulltext store on /Volumes/Cherry6TB — per-year counts + 0-byte
#   C. xCard extractions (raw + distilled markdown variants)
#   D. 22-topic Genesis classification (jsonl outputs + low_conf + err)
#   E. osti-replication-candidates work tree (commits + result counts)
#   F. recon / broken-pointer recovery (scripts + result logs)
#   G. OCR pipeline (script + remote result store on rbdgx2)
#   H. live subprocesses touching the corpus (active runs)
#
# Output: structured plain-text to /tmp/osti-inventory.log. Pipe into a
# summary in chat or hand to a teammate. Re-runnable; no side effects.
#
# Sandbox-safe quirks:
#   - Cherry6TB nested layout is `{year}/{year}/*.pdf` (not `{year}/*.pdf`)
#   - sqlite column is `year` (TEXT), NOT `publication_year`
#   - Cherry6TB root scan hangs 60s+ — script avoids `ls /Volumes/Cherry6TB/`
#   - rbdgx2 reachable via `cels-rbdgx2` SSH alias (never bare `rbdgx2`)
#
# Usage:
#   bash ~/.hermes/skills/research/osti-corpus-fetch/scripts/osti_corpus_status.sh \
#        | tee /tmp/osti-inventory.log

set -u
echo "==== TIMESTAMP ===="
date -u +"%Y-%m-%dT%H:%M:%SZ"
echo ""

echo "==== A. CONTACTS DATABASE (paper metadata + DOIs) ===="
DB=/Users/stevens/Dropbox/XFER/osti-contacts/contacts.db
if [ -f "$DB" ]; then
    ls -la "$DB"
    sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table';" 2>/dev/null
    echo "--- paper count by year (column is 'year', TEXT) ---"
    sqlite3 "$DB" "SELECT year, COUNT(*) FROM paper WHERE year IS NOT NULL GROUP BY year ORDER BY year DESC LIMIT 30;" 2>/dev/null
    echo "--- total papers ---"
    sqlite3 "$DB" "SELECT COUNT(*) FROM paper;" 2>/dev/null
    echo "--- papers with DOI ---"
    sqlite3 "$DB" "SELECT COUNT(*) FROM paper WHERE doi IS NOT NULL AND doi != '';" 2>/dev/null
    echo "--- papers with journal ---"
    sqlite3 "$DB" "SELECT COUNT(*) FROM paper WHERE journal IS NOT NULL AND journal != '';" 2>/dev/null
fi
echo ""

echo "==== B. PDF FULLTEXT STORE (/Volumes/Cherry6TB) ===="
PDFROOT=/Volumes/Cherry6TB/osti_fulltext
if [ -d "$PDFROOT" ]; then
    # NOTE: nested layout — /Volumes/Cherry6TB/osti_fulltext/<year>/<year>/*.pdf
    # NOT /Volumes/Cherry6TB/osti_fulltext/<year>/*.pdf
    total=0
    empty_total=0
    for yr in 2016 2017 2018 2019 2020 2021 2022 2023 2024 2025; do
        inner="$PDFROOT/$yr/$yr"
        if [ -d "$inner" ]; then
            n=$(ls -1 "$inner" 2>/dev/null | wc -l | tr -d ' ')
            e=$(find "$inner" -type f -size 0 2>/dev/null | wc -l | tr -d ' ')
            total=$((total + n))
            empty_total=$((empty_total + e))
            echo "  $yr: $n PDFs, $e empty (0-byte)"
        fi
    done
    echo "  TOTAL: $total PDFs, $empty_total empty (re-fetch candidates)"
fi
echo ""

echo "==== C. xCard EXTRACTIONS (text -> structured cards) ===="
GOOD=/Users/stevens/Dropbox/ARGONNE-PAPERS/GOOD
XCARDS=/Users/stevens/Dropbox/ARGONNE-PAPERS/XCARDS
echo "--- raw extractions (one per paper, includes NO_SIGNALS) ---"
for variant in DATA MODEL AGENT; do
    dir="$GOOD/ALL-PAPERS-${variant}-CARDS"
    if [ -d "$dir" ]; then
        count=$(ls -1 "$dir" 2>/dev/null | wc -l | tr -d ' ')
        echo "  ALL-PAPERS-${variant}-CARDS: $count files"
    fi
done
echo "--- distilled markdown cards (signal-bearing only) ---"
for variant in DATA MODEL AGENT; do
    dir="$XCARDS/MARKDOWN-${variant}-CARDS"
    if [ -d "$dir" ]; then
        count=$(ls -1 "$dir" 2>/dev/null | wc -l | tr -d ' ')
        echo "  MARKDOWN-${variant}-CARDS: $count files"
    fi
done
echo ""

echo "==== D. CLASSIFICATION OUTPUT (22-topic Genesis classifier) ===="
CLS=/Users/stevens/code/implicit-models
if [ -d "$CLS" ]; then
    cd "$CLS"
    echo "--- classifier outputs (newest first) ---"
    ls -lat *.jsonl 2>/dev/null | head -10
    echo ""
    for f in classifications.jsonl classifications_v2add.jsonl code_extractions.jsonl; do
        if [ -f "$f" ]; then
            echo "  $(wc -l < $f) lines  $f"
        fi
    done
    echo ""
    echo "--- latest run final summary (if log present) ---"
    grep FINAL classifier_v2add.stdout.log 2>/dev/null | tail -3
    echo ""
    echo "--- distinct osti_ids classified (union across runs) ---"
    cat classifications.jsonl classifications_v2add.jsonl 2>/dev/null | python3 -c "
import sys, json
ids, total, low, err = set(), 0, 0, 0
for line in sys.stdin:
    try:
        r = json.loads(line); total += 1
        ids.add(r.get('osti_id'))
        if r.get('low_conf'): low += 1
        if 'error' in r or not r.get('topics'): err += 1
    except: pass
print(f'  total records: {total:,}')
print(f'  unique osti_ids: {len(ids):,}')
print(f'  low_conf: {low:,}')
print(f'  err or no topics: {err:,}')
print(f'  good: {total - low - err:,}')
" 2>/dev/null
fi
echo ""

echo "==== E. OSTI REPLICATION CANDIDATES (working tree) ===="
REPL=/Users/stevens/code/osti-replication-candidates
if [ -d "$REPL" ]; then
    cd "$REPL"
    git log --oneline -3 2>/dev/null
    echo "--- web validation sample status ---"
    if [ -f tests/results_web.jsonl ]; then
        wc -l tests/results_web.jsonl
    fi
fi
echo ""

echo "==== F. RECON / BROKEN-POINTER RECOVERY ===="
echo "--- recon scripts ---"
ls "$REPL"/recon*.py "$REPL"/probe_*.py 2>/dev/null
echo "--- recon result logs ---"
ls -lat "$REPL"/*.log 2>/dev/null | head -10
echo "--- recon_v2 cells (per-lab × per-year) ---"
ls "$REPL"/recon_v2/ 2>/dev/null | wc -l
echo "--- rescue_report.csv stats ---"
if [ -f "$PDFROOT/rescue_report.csv" ]; then
    python3 -c "
import csv
counts = {}
with open('$PDFROOT/rescue_report.csv') as f:
    r = csv.DictReader(f)
    for row in r:
        counts[row.get('status','?')] = counts.get(row.get('status','?'), 0) + 1
for s, c in sorted(counts.items(), key=lambda x: -x[1]):
    print(f'  {s}: {c}')
" 2>/dev/null
fi
echo ""

echo "==== G. OCR / IMAGE-PDF -> MARKDOWN PIPELINE ===="
echo "--- pipeline scripts ---"
find ~/code ~/Dropbox/ARGONNE-PAPERS -maxdepth 4 -type f \( -name "*ocr*" -o -name "marker*" -o -name "pdf2txt*" \) 2>/dev/null | head -10
echo ""
echo "--- remote OCR result store (cels-rbdgx2:/rbstor/stevens/osti_fulltext_v2_md/) ---"
timeout 15 ssh -o ConnectTimeout=8 cels-rbdgx2 "
  echo md_files=\$(ls /rbstor/stevens/osti_fulltext_v2_md/*.md 2>/dev/null | wc -l)
  echo total_size=\$(du -sh /rbstor/stevens/osti_fulltext_v2_md/ 2>/dev/null | cut -f1)
  echo queue_remaining=\$(wc -l < /home/stevens/ocr_queue.txt 2>/dev/null)
  echo 'recent marker activity (last 3):'
  tail -3 /rbstor/stevens/osti_fulltext_v2_md/marker_run.gpu0.jsonl 2>/dev/null
" 2>&1 | sed 's/^/  /'
echo ""

echo "==== H. ACTIVE SUBPROCESSES TOUCHING OSTI ===="
ps aux | grep -E '(osti|extract|classify|xcard|argonne|marker)' | grep -v grep | head -10
echo ""

echo "==== UMP BULK IMPORT (last load summary) ===="
if [ -f "$REPL/ump_bulk_import.log" ]; then
    grep -E '^DONE:' "$REPL/ump_bulk_import.log" 2>/dev/null | tail -3
fi
echo ""

echo "==== DONE ===="
