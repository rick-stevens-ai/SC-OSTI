#!/bin/bash
# Mirror cels-rbdgx2 Marker outputs to Cherry6TB daily.
# Quiet by default (no_agent watchdog pattern) - only outputs on error.
#
# Install:
#   cp scripts/marker_mirror.sh ~/.hermes/scripts/
#   chmod +x ~/.hermes/scripts/marker_mirror.sh
# Schedule (Hermes cron):
#   cronjob create no_agent=true schedule='0 4 * * *' \
#     script=marker_mirror.sh name=marker-mirror-cels-to-cherry deliver=local

set -e
SRC="cels-rbdgx2:/rbstor/stevens/osti_fulltext_v2_md/"
DST="/Volumes/Cherry6TB/osti_fulltext_v2_md/"

if [ ! -d "$(dirname $DST)" ]; then
    echo "ERROR: Cherry6TB not mounted at /Volumes/Cherry6TB"
    exit 1
fi

mkdir -p "$DST"
rsync -aq --delete-after "$SRC" "$DST" 2>&1
EXIT=$?

if [ $EXIT -ne 0 ]; then
    echo "ERROR: rsync exit $EXIT"
    exit $EXIT
fi

# Silent on success
exit 0
