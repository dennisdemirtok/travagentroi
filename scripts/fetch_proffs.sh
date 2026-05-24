#!/bin/bash
# Fetch proffs consensus data from kungenstrav.se
# Run 3x/day: 13:00 (pre-race), 17:30 (pre-race), 23:00 (results)
cd /Users/dennisdemirtok/trading/trav-agent

echo "$(date): Fetching proffs data..."
.venv/bin/python fetch_proffs_data.py >> /tmp/trav-proffs-fetch.log 2>&1

echo "$(date): Checking for results..."
.venv/bin/python fetch_proffs_data.py --results >> /tmp/trav-proffs-fetch.log 2>&1

echo "$(date): Done."
