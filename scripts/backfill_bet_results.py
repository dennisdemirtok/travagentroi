"""Backfill bet_results till Supabase.

Kör alla cachade omgångar genom analyspipelinen och syncar
vinnarspel-kandidater till Supabase bet_results-tabellen.

Kör:
    python3 scripts/backfill_bet_results.py
    python3 scripts/backfill_bet_results.py --from 2025-06-01
"""

import asyncio
import re
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.data.atg_client import ATGClient
from trav_agent.backtest.runner import BacktestRunner
from trav_agent.config import CACHE_DIR, SUPABASE_ENABLED


def discover_rounds(game_types=None, min_date="2025-06-01"):
    if game_types is None:
        game_types = ["V86", "V85", "V75", "GS75"]
    results = set()
    for gt in game_types:
        pattern = re.compile(rf"^{gt}_(\d{{4}}-\d{{2}}-\d{{2}})_\d+_\d+_[a-f0-9]+\.json$")
        if CACHE_DIR.exists():
            for f in CACHE_DIR.iterdir():
                m = pattern.match(f.name)
                if m and m.group(1) >= min_date:
                    results.add((gt, m.group(1)))
    return sorted(results, key=lambda x: (x[1], x[0]))


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default="2025-06-01")
    args = parser.parse_args()

    if not SUPABASE_ENABLED:
        print("ERROR: Supabase not configured. Set SUPABASE_URL and SUPABASE_KEY in .env")
        return

    from trav_agent.database.bet_sync import sync_bet_results

    rounds_meta = discover_rounds(min_date=args.from_date)
    print(f"Found {len(rounds_meta)} rounds from {args.from_date}\n")

    client = ATGClient()
    analyzer = CompositeAnalyzer()

    synced = 0
    failed = 0

    for i, (gt, date_str) in enumerate(rounds_meta):
        d = date.fromisoformat(date_str)
        try:
            gr = await client.fetch_full_round(gt, d)
            if not gr:
                continue

            has_results = all(bool(r.result_order) for r in gr.races)
            if not has_results:
                continue

            # Leakage prevention
            BacktestRunner._filter_future_starts(gr)
            BacktestRunner._neutralize_closing_odds(gr)

            analyzer.analyze_round(gr)

            try:
                sync_bet_results(gr)
                synced += 1
                if synced % 10 == 0:
                    print(f"  [{synced}] Synced up to {date_str} {gt}")
            except Exception as e:
                failed += 1
                print(f"  SYNC ERROR {date_str} {gt}: {e}")

        except Exception as e:
            failed += 1
            continue

    print(f"\nDone: {synced} rounds synced, {failed} failed")


if __name__ == "__main__":
    asyncio.run(main())
