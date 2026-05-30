"""Incremental backlog update — generate entries for new dates and merge.

Extends backlog.json with entries for dates not yet covered.

Usage:
    python update_backlog.py --from 2026-04-26 --to 2026-05-23
    python update_backlog.py --from 2026-04-26 --to 2026-05-23 --dry-run
"""

import argparse
import asyncio
import gzip
import json
import re
import sys
from datetime import date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.betting.system_generator import SystemGenerator
from trav_agent.backtest.runner import BacktestRunner
sys.path.insert(0, str(Path(__file__).parent / "analysis_scripts"))
from roi_backtest_integrated import count_correct, _count_winning_rows_in_system

# Game types to include
BACKLOG_GAME_TYPES = ["V75", "V85", "GS75", "V86", "V64"]

# Strategies matching current generate_backlog.py — only D_smart variants.
# Old A_union/B_marknad/C_bred and fake I_streck/Q_dom/D_market strategies are
# removed (all were A_union under the hood due to data leakage).
STRATEGIES = [
    # (strategy, filter, budget, max_spikes, spike_conf, spike_gap, label)
    ("D_smart",   "none", 500,  0, 0, 0, "D_smart_500"),
    ("D_smart",   "none", 1000, 0, 0, 0, "D_smart_1000"),
    ("D_smart",   "none", 1500, 0, 0, 0, "D_smart_1500"),
    ("D_smart",   "none", 2000, 0, 0, 0, "D_smart_2000"),
]


def _get_track_name(cache_dir: Path, game_type: str, game_date: str) -> str:
    """Get track name from cache file."""
    for f in cache_dir.glob(f"{game_type}_{game_date}_*.json"):
        try:
            data = json.load(open(f))
            races = data.get("races", [])
            if races:
                return races[0].get("track", {}).get("name", "?")
        except Exception:
            pass
    return "?"


def _process_round(gr, system, cache_dir):
    """Process a round with a system and return entry dict."""
    game_type = gr.game_type
    game_date = str(gr.round_date)
    track_name = _get_track_name(cache_dir, game_type, game_date)

    race_results = [race.result_order for race in gr.races]
    num_races = len(gr.races)
    num_correct = count_correct(system.race_picks, race_results)

    payout_per_row = gr.dividends.get(num_correct, 0.0)
    winning_rows = 0
    total_payout = 0.0

    if payout_per_row > 0:
        winning_rows = _count_winning_rows_in_system(
            system.race_picks, race_results, num_correct
        )
        total_payout = payout_per_row * winning_rows

    # Detailed picks per race
    race_details = []
    for race, rp in zip(gr.races, system.race_picks):
        winner = race.result_order[0] if race.result_order else None
        winner_name = ""
        winner_streck = 0.0
        if winner:
            for e in race.active_entries:
                if e.post_position == winner:
                    winner_name = e.horse.name
                    winner_streck = e.bet_percentage or 0
                    break

        race_details.append({
            "avd": rp.race_number,
            "dist": rp.distance,
            "metod": rp.start_method,
            "startande": rp.num_starters,
            "conf": round(rp.confidence, 0),
            "upset": round(rp.upset_risk, 0),
            "picks": rp.num_picks,
            "pick_list": [
                {"nr": nr, "namn": namn[:16], "score": round(sc, 1), "streck": round(st * 100, 1)}
                for nr, namn, sc, st in zip(rp.picks, rp.pick_names, rp.pick_scores, rp.pick_streck)
            ],
            "tackning": round(rp.combined_streck * 100, 0),
            "vinnare": winner,
            "vinnare_namn": winner_name[:16] if winner_name else "",
            "vinnare_streck": round(winner_streck * 100, 1),
            "ratt": winner is not None and winner in rp.picks,
        })

    return {
        "date": game_date,
        "game_type": game_type,
        "track": track_name,
        "strategy": system.strategy,
        "cost": round(system.total_cost, 0),
        "rows": system.total_rows,
        "num_correct": num_correct,
        "num_races": num_races,
        "hit": num_correct == num_races,
        "payout": round(total_payout, 0),
        "payout_per_row": round(payout_per_row, 0),
        "winning_rows": winning_rows,
        "avg_confidence": round(system.avg_confidence, 1),
        "avg_upset": round(system.avg_upset_risk, 1),
        "races": race_details,
    }


async def main():
    parser = argparse.ArgumentParser(description="Update backlog.json incrementally")
    parser.add_argument("--from", dest="from_date", required=True,
                        help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True,
                        help="End date YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be generated without modifying backlog.json")
    args = parser.parse_args()

    start_date = args.from_date
    end_date = args.to_date

    cache_dir = Path("cache")
    backlog_path = Path("backlog.json")
    backlog_gz_path = Path("backlog.json.gz")

    # 1. Load existing backlog
    if backlog_path.exists():
        with open(backlog_path) as f:
            backlog = json.load(f)
        existing_entries = backlog.get("entries", [])
        print(f"Loaded existing backlog: {len(existing_entries)} entries")
    else:
        backlog = {"entries": [], "strategies": {}, "generated": str(date.today())}
        existing_entries = []
        print("No existing backlog found, creating new one")

    # 2. Find dates to process from cache
    all_dates: dict[str, set[str]] = {gt: set() for gt in BACKLOG_GAME_TYPES}
    pattern = re.compile(
        r"^(" + "|".join(BACKLOG_GAME_TYPES) + r")_(\d{4}-\d{2}-\d{2})_"
    )
    for f in cache_dir.iterdir():
        m = pattern.match(f.name)
        if m:
            d = m.group(2)
            if start_date <= d <= end_date:
                all_dates[m.group(1)].add(d)

    dates_to_load = []
    for gt in BACKLOG_GAME_TYPES:
        for d in sorted(all_dates[gt]):
            dates_to_load.append((gt, d))

    print(f"\nRounds to process ({start_date} to {end_date}):")
    for gt in BACKLOG_GAME_TYPES:
        if all_dates[gt]:
            print(f"  {gt}: {len(all_dates[gt])} rounds")
    print(f"  Total: {len(dates_to_load)} rounds")

    if not dates_to_load:
        print("No new rounds found in cache for the specified date range.")
        return

    # 3. Load rounds (fetch from cache, then refresh results from API)
    client = ATGClient()
    analyzer = CompositeAnalyzer()

    rounds = []
    for game_type, game_date in dates_to_load:
        try:
            gr = await client.fetch_full_round(game_type, date.fromisoformat(game_date))
            if gr and gr.races:
                # If not finished (cached from before results), refresh from API
                if not gr.is_finished:
                    print(f"  Refreshing results for {game_type} {game_date}...", end="")
                    n_odds, n_fin, has_div = await client.refresh_results(gr)
                    if gr.is_finished:
                        print(f" OK ({n_fin} races finished, dividends: {'yes' if has_div else 'no'})")
                    else:
                        finished = sum(1 for r in gr.races if r.result_order)
                        total = len(gr.races)
                        print(f" partial ({finished}/{total} races finished)")

                if gr.is_finished:
                    rounds.append(gr)
                else:
                    # Check if we have result_order for all races even if status
                    # didn't update (sometimes status stays "upcoming" but results exist)
                    has_all_results = all(r.result_order for r in gr.races)
                    if has_all_results and gr.dividends:
                        print(f"  {game_type} {game_date}: has all results despite status, including")
                        rounds.append(gr)
                    else:
                        finished = sum(1 for r in gr.races if r.result_order)
                        total = len(gr.races)
                        print(f"  Skipping {game_type} {game_date} - not finished "
                              f"({finished}/{total} races with results, "
                              f"dividends: {'yes' if gr.dividends else 'no'})")
        except Exception as e:
            print(f"  Error loading {game_type} {game_date}: {e}")
            continue

    print(f"\nLoaded {len(rounds)} finished rounds")

    if not rounds:
        print("No finished rounds to process.")
        return

    # 4. Temporal filtering to prevent data leakage
    for gr in rounds:
        BacktestRunner._filter_future_starts(gr)
        for race in gr.races:
            for entry in race.entries:
                entry.horse.recompute_career_from_starts()

    for gr in rounds:
        analyzer.analyze_round(gr)
    print("All rounds analyzed (temporal filtering active)\n")

    # 5. Generate entries for each strategy
    new_entries = []

    for strategy, filt, budget, max_spikes, spike_conf, spike_gap, label in STRATEGIES:
        gen = SystemGenerator(
            budget=budget, strategy=strategy, selective_filter=filt,
            max_spikes=max_spikes, spike_conf_threshold=spike_conf,
            spike_score_gap=spike_gap,
        )

        strat_count = 0
        strat_hits = 0

        for gr in rounds:
            system = gen.generate(gr)
            if system.skip_round:
                continue

            entry = _process_round(gr, system, cache_dir)
            entry["strategy"] = label
            new_entries.append(entry)
            strat_count += 1

            if entry["payout"] > 0:
                strat_hits += 1
                r = entry["num_correct"]
                n = entry["num_races"]
                status = f"{'+++' if r == n else '+'} {r}/{n}"
                print(f"  [{label[:15]}] {entry['date']} {entry['game_type']} "
                      f"{entry['track'][:10]} {status} -> {entry['payout']:,.0f} kr")

        total_cost = sum(e["cost"] for e in new_entries if e["strategy"] == label)
        total_payout = sum(e["payout"] for e in new_entries if e["strategy"] == label)
        roi = (total_payout - total_cost) / total_cost * 100 if total_cost > 0 else 0
        print(f"  {label}: {strat_count} rounds, {strat_hits} hits, "
              f"ROI: {roi:+.1f}%, Netto: {total_payout - total_cost:+,.0f} kr\n")

    print(f"Generated {len(new_entries)} new entries")

    if args.dry_run:
        print("\n[DRY RUN] Would add these entries to backlog.json. Exiting.")
        return

    # 6. Remove any existing entries for the same date/game_type/strategy combos
    new_keys = set()
    for e in new_entries:
        new_keys.add((e["date"], e["game_type"], e["strategy"]))

    kept = [e for e in existing_entries
            if (e["date"], e["game_type"], e["strategy"]) not in new_keys]
    removed = len(existing_entries) - len(kept)
    if removed > 0:
        print(f"Removed {removed} existing entries that overlap with new data")

    # 7. Merge and sort (newest first)
    all_entries = new_entries + kept
    all_entries.sort(key=lambda e: (e["date"], e["strategy"]), reverse=True)

    backlog["entries"] = all_entries
    backlog["generated"] = str(date.today())

    # 8. Save
    with open(backlog_path, "w") as f:
        json.dump(backlog, f, indent=2, default=str)
    print(f"\nSaved backlog.json ({len(all_entries)} entries)")

    # 9. Compress to .gz
    with open(backlog_path, "rb") as f_in:
        with gzip.open(backlog_gz_path, "wb") as f_out:
            f_out.write(f_in.read())
    gz_size = backlog_gz_path.stat().st_size
    json_size = backlog_path.stat().st_size
    print(f"Compressed to backlog.json.gz ({gz_size / 1024 / 1024:.1f} MB, "
          f"ratio: {gz_size / json_size:.1%})")


if __name__ == "__main__":
    asyncio.run(main())
