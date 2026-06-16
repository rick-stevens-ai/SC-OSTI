#!/usr/bin/env bash
# wrapper_start_exit.sh — file-first START/EXIT wrapper for long-running
# pipeline scripts launched over two-hop SSH (m1 → cherryrd → aurora UAN,
# m1 → cels jumpbox → compute, etc.).
#
# Why this exists: when a Python process is launched on a remote host via
# two-hop SSH and you poll its log file across the chain, the log can read
# as empty for 30-60s after the process actually started writing because
# stdout buffering at multiple layers (Python, inner SSH, outer SSH) plus
# remote filesystem flush latency conspire to delay visibility. Without a
# START marker that lands on disk BEFORE the Python imports run, the
# polling agent declares the process dead and relaunches — burning launch
# cycles and creating zombie/orphan processes.
#
# This wrapper writes a START line to a status file as its very first
# action, runs the work with PYTHONUNBUFFERED=1, and writes an EXIT line
# on completion (success or failure). The polling agent can:
#   - check wrapper.status for the live state ("RUNNING <ts>" / "EXIT <rc> <ts>")
#   - check wrapper.pid for the python process PID
#   - tail wrapper.log for live progress (now properly unbuffered)
#   - confirm completion via wrapper.end
#
# Usage:
#   ./wrapper_start_exit.sh <outdir> <python-args...>
#
# e.g. as part of bulk_fetch_launcher_template.py pilot:
#   ./wrapper_start_exit.sh ./run_pilot_500 \
#       --manifest manifest.jsonl --outdir ./run_pilot_500 --limit 500
#
# Produces in $OUTDIR:
#   wrapper.start    ISO ts + cmdline of START event (written FIRST)
#   wrapper.log      interleaved stdout+stderr from the python process
#   wrapper.pid      PID of the python process (cleared on exit)
#   wrapper.status   "RUNNING <ts>" while alive; "EXIT <rc> <ts>" on exit
#   wrapper.end      ISO ts + exit code + start/end timestamps
#
# Configuration via env vars:
#   PYTHON_BIN       Python interpreter (default: /usr/bin/python3.10)
#   LAUNCHER         Script to invoke (default: ${SCRIPT_DIR}/osti_bulk_fetch.py)
#
# Reference: data-science/corpus-structured-extraction/SKILL.md and
# devops/kukla-self-operations/SKILL.md "Pitfall: two-hop SSH stdout
# buffering" section.

set -u

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <outdir> <python-args...>" >&2
    exit 2
fi

OUTDIR="$1"; shift
mkdir -p "$OUTDIR"
OUTDIR_ABS="$(cd "$OUTDIR" && pwd)"

START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.10}"
LAUNCHER="${LAUNCHER:-${SCRIPT_DIR}/osti_bulk_fetch.py}"

# Write START FIRST — before any python import latency.
# This is the load-bearing detail. A polling reader sees life immediately,
# even before the Python interpreter cold-starts.
{
    echo "=== START $START_TS ==="
    echo "host:       $(hostname)"
    echo "pwd:        $(pwd)"
    echo "user:       $(whoami)"
    echo "wrapper:    $0"
    echo "launcher:   $LAUNCHER"
    echo "python:     $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"
    echo "outdir:     $OUTDIR_ABS"
    echo "args:       $*"
} > "$OUTDIR_ABS/wrapper.start"

# Mirror START into wrapper.log so a single-file reader sees it
cp "$OUTDIR_ABS/wrapper.start" "$OUTDIR_ABS/wrapper.log"
echo "RUNNING $START_TS" > "$OUTDIR_ABS/wrapper.status"

cleanup() {
    local rc=$?
    local end_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    {
        echo "=== END $end_ts ==="
        echo "exit_code:  $rc"
        echo "start:      $START_TS"
        echo "end:        $end_ts"
    } > "$OUTDIR_ABS/wrapper.end"
    {
        echo ""
        cat "$OUTDIR_ABS/wrapper.end"
    } >> "$OUTDIR_ABS/wrapper.log"
    echo "EXIT $rc at $end_ts" > "$OUTDIR_ABS/wrapper.status"
    rm -f "$OUTDIR_ABS/wrapper.pid"
}
trap cleanup EXIT

# Launch python in the foreground (the wrapper itself is what gets
# nohup'd / disowned by the caller). Capture PID and stream output.
export PYTHONUNBUFFERED=1
"$PYTHON_BIN" -u "$LAUNCHER" "$@" >> "$OUTDIR_ABS/wrapper.log" 2>&1 &
PYPID=$!
echo "$PYPID" > "$OUTDIR_ABS/wrapper.pid"

wait "$PYPID"
exit $?
