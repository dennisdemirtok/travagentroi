#!/bin/bash
cd /Users/dennisdemirtok/trading/trav-agent
python3 -m trav_agent fetch-upcoming --days 7 >> /tmp/trav-agent-fetch.log 2>&1
