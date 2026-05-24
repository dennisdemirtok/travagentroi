#!/usr/bin/env python3
"""Quick backtest: v7 Dennis-method model on cached data.

Tests the new 13-factor model with Dennis's signals:
- Rebuilt time analysis (last 5 starts, distance projection)
- Last win factor
- Competition strength (class drop detection)
- Layoff factor
"""

import sys
import os
import json
import re
import random
import logging
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.analysis.system_builder import build_system, classify_leg
from trav_agent.config import DEFAULT_CONFIG, CACHE_DIR

logging.basicConfig(level=logging.WARNING)


def find_cached_rounds(game_type: str = "V75", max_rounds: int = 80):
    """Find cached game rounds by scanning calendar files."""
    rounds = []
    calendar_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})_[a-f0-9]+\.json$")

    for fname in sorted(os.listdir(CACHE_DIR)):
        m = calendar_pattern.match(fname)
        if not m:
            continue

        day = m.group(1)
        cal_path = CACHE_DIR / fname

        try:
            with open(cal_path) as f:
                cal = json.load(f)
        except Exception:
            continue

        # Look for V75/V85 games in calendar
        games = cal.get("games", {})
        if isinstance(games, dict):
            # New format: games is dict {game_type: [list of game objects]}
            for gt_key in ["V75", "V85"]:
                game_list = games.get(gt_key, [])
                for game in game_list:
                    if isinstance(game, dict):
                        gid = game.get("id", "")
                        if gid:
                            rounds.append((day, gid))
        elif isinstance(games, list):
            for game in games:
                if isinstance(game, dict):
                    gid = game.get("id", "")
                    if gid.startswith("V75") or gid.startswith("V85"):
                        rounds.append((day, gid))

        # Also check tracks for game info
        tracks = cal.get("tracks", [])
        for track in tracks:
            if not isinstance(track, dict):
                continue
            bgt = track.get("biggestGameType", "")
            if bgt in ("V75", "V85"):
                # Construct a game_id from track info
                track_id = track.get("id", 0)
                # Look in the games dict
                pass  # Already handled above

    # Deduplicate by day
    seen_days = set()
    unique = []
    for day, gid in rounds:
        if day not in seen_days:
            seen_days.add(day)
            unique.append((day, gid))

    # Sample if too many (deterministic for reproducibility)
    if len(unique) > max_rounds:
        random.seed(42)
        unique = random.sample(unique, max_rounds)
        unique.sort()

    return unique


async def run_backtest():
    """Run v7 backtest on cached rounds."""
    client = ATGClient()

    # Find rounds
    rounds = find_cached_rounds("V75", max_rounds=60)
    print(f"Hittade {len(rounds)} V75/V85-omgangar i cache")

    if not rounds:
        print("Inga omgangar hittade. Avbryter.")
        return

    # Filter to 2024+ for more relevant data
    rounds_2024 = [(d, g) for d, g in rounds if d >= "2024-01-01"]
    if len(rounds_2024) >= 20:
        rounds = rounds_2024
        print(f"Filtrerat till {len(rounds)} omgangar fran 2024+")

    analyzer = CompositeAnalyzer()

    # Stats
    total_races = 0
    total_entries = 0
    top1_hits = 0
    top2_hits = 0
    top3_hits = 0
    spik_total = 0
    spik_hits = 0

    # Per-factor stats
    factor_top1_scores = defaultdict(list)
    factor_other_scores = defaultdict(list)

    # System building stats
    system_legs_total = 0
    system_legs_hit = 0
    system_full_hits = 0  # All legs correct in a round

    for day, gid in rounds:
        # Parse game_id to get game_type and date
        parts = gid.split("_")
        if len(parts) < 2:
            continue
        gt = parts[0]

        try:
            from datetime import date as dt_date
            day_date = dt_date.fromisoformat(day) if isinstance(day, str) else day
            game_round = await client.fetch_full_round(gt, day_date)
        except Exception as e:
            print(f"  Skippar {day}: {e}")
            continue

        if not game_round or not game_round.races:
            continue

        # Temporal filtering: remove future starts from past_starts
        from datetime import date as dt_date
        race_date = game_round.round_date
        if isinstance(race_date, str):
            race_date = dt_date.fromisoformat(race_date)

        for race in game_round.races:
            # Ensure race_date is a date object
            if isinstance(race.race_date, str):
                race.race_date = dt_date.fromisoformat(str(race.race_date))

            for entry in race.entries:
                before = len(entry.horse.past_starts)
                entry.horse.past_starts = [
                    s for s in entry.horse.past_starts
                    if (s.start_date if isinstance(s.start_date, dt_date) else dt_date.fromisoformat(str(s.start_date))) < race_date
                ]
                entry.horse.recompute_career_from_starts()
                # Neutralize closing odds
                entry.bet_percentage = None

        # Analyze
        analyzer.analyze_round(game_round)

        # Build system
        system_plan = build_system(game_round, budget=200)

        all_legs_hit = True
        for race in game_round.races:
            if not race.result_order or not race.active_entries:
                continue

            winner = race.result_order[0] if race.result_order else None
            if not winner:
                continue

            total_races += 1
            sorted_entries = sorted(
                race.active_entries, key=lambda e: e.super_score, reverse=True
            )

            if not sorted_entries:
                continue

            total_entries += len(sorted_entries)

            # Top-N accuracy
            our_ranking = [e.post_position for e in sorted_entries]
            if winner == our_ranking[0]:
                top1_hits += 1
            if winner in our_ranking[:2]:
                top2_hits += 1
            if winner in our_ranking[:3]:
                top3_hits += 1

            # Spike accuracy
            spik_entry = next(
                (e for e in sorted_entries if e.recommendation == "spik"), None
            )
            if spik_entry:
                spik_total += 1
                if spik_entry.post_position == winner:
                    spik_hits += 1

            # Per-factor analysis: how does each factor score winners?
            for entry in sorted_entries:
                is_winner = entry.post_position == winner
                for fname, fscore in entry.factor_scores.items():
                    if is_winner:
                        factor_top1_scores[fname].append(fscore)
                    else:
                        factor_other_scores[fname].append(fscore)

            # System leg check
            leg = next(
                (l for l in system_plan.legs if l.race_number == race.race_number),
                None,
            )
            if leg:
                system_legs_total += 1
                if winner in leg.picks[:leg.num_picks]:
                    system_legs_hit += 1
                else:
                    all_legs_hit = False
            else:
                all_legs_hit = False

        if all_legs_hit and len(game_round.races) >= 7:
            system_full_hits += 1

    # ── Results ─────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"V7 Dennis-Method Backtest Resultat")
    print(f"{'='*60}")
    print(f"Lopp analyserade: {total_races}")
    print(f"Omgangar: {len(rounds)}")
    print(f"Snitt starter/lopp: {total_entries/max(total_races,1):.1f}")
    print()

    if total_races > 0:
        print(f"Top-1 traffsäkerhet: {top1_hits}/{total_races} = {100*top1_hits/total_races:.1f}%")
        print(f"Top-2 traffsäkerhet: {top2_hits}/{total_races} = {100*top2_hits/total_races:.1f}%")
        print(f"Top-3 traffsäkerhet: {top3_hits}/{total_races} = {100*top3_hits/total_races:.1f}%")
        print()
        if spik_total > 0:
            print(f"Spikar: {spik_hits}/{spik_total} = {100*spik_hits/spik_total:.1f}%")
        else:
            print("Inga spikar tilldelade")

    # System stats
    print(f"\n{'─'*60}")
    print(f"SYSTEMBYGGARE RESULTAT")
    print(f"{'─'*60}")
    if system_legs_total > 0:
        print(f"Avdelnings-traffar: {system_legs_hit}/{system_legs_total} = {100*system_legs_hit/system_legs_total:.1f}%")
    print(f"Hela system-traffar: {system_full_hits}/{len(rounds)}")

    # Factor contribution analysis
    print(f"\n{'─'*60}")
    print(f"FAKTORANALYS (snitt score vinnare vs övriga)")
    print(f"{'─'*60}")
    print(f"{'Faktor':25s} {'Vinnare':>10s} {'Övriga':>10s} {'Diff':>8s} {'Lift':>7s}")
    print(f"{'─'*60}")

    factor_lifts = []
    for fname in sorted(factor_top1_scores.keys()):
        w_scores = factor_top1_scores[fname]
        o_scores = factor_other_scores[fname]
        if w_scores and o_scores:
            w_avg = sum(w_scores) / len(w_scores)
            o_avg = sum(o_scores) / len(o_scores)
            diff = w_avg - o_avg
            lift = w_avg / max(o_avg, 1)
            factor_lifts.append((fname, w_avg, o_avg, diff, lift))
            print(f"{fname:25s} {w_avg:10.1f} {o_avg:10.1f} {diff:+8.1f} {lift:7.2f}x")

    # Sort by diff to see most predictive
    print(f"\nRankad efter prediktivitet:")
    for fname, w_avg, o_avg, diff, lift in sorted(factor_lifts, key=lambda x: -x[3]):
        bar = "█" * int(max(0, diff))
        print(f"  {fname:25s} {diff:+6.1f} {bar}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_backtest())
