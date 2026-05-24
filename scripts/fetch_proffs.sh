#!/bin/bash
# Fetch professional tipster data from beta.kungenstrav.se
#
# Pre-race: captures viktat proffsstreck (weighted consensus from 73+ tipsters).
#   Systems are ONLY available before races start — historical games return empty.
#   Run at 12:00 and 14:00 to catch both weekend (15:00) and weekday (19:30) starts.
#
# Post-race: merges actual race outcomes (winner, placements, final odds).
#   Run at 22:00 after evening races complete.
#
# Usage:
#   ./fetch_proffs.sh           — fetch pre-race picks
#   ./fetch_proffs.sh --results — merge post-race outcomes

cd /Users/dennisdemirtok/trading/trav-agent

LOG=/tmp/trav-proffs-fetch.log

if [ "$1" = "--results" ]; then
    echo "$(date): Fetching results for completed games..." >> "$LOG"
    .venv/bin/python3 fetch_proffs_data.py --results >> "$LOG" 2>&1
else
    echo "$(date): Fetching pre-race proffs picks..." >> "$LOG"
    .venv/bin/python3 fetch_proffs_data.py >> "$LOG" 2>&1
fi

echo "$(date): Done ($1)." >> "$LOG"
