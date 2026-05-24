#!/usr/bin/env python3
"""Empirisk analys av galopphistorik + voltspår + tillägg-positioner.

Dennis:
- Voltlopp gynnas spår 1, 6, 7 (springspår)
- Galopprisken ökar spår 2, 4 i volt (trångt)
- Häst på tillägg med nr 13 kan ha springspår i andra volten
- Favorit som galopperat → ökad skrällrisk
- Skräll som galopperat → oförutsägbar, kan överraska

Analyserar ~290 omgångar, ~1900 lopp med temporal filtrering.
"""

import asyncio
import json
import glob
import re
import sys
import math
import random
import time
from datetime import date
from pathlib import Path
from collections import defaultdict
import statistics

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.data.models import GameRound, Race, StartMethod
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import DEFAULT_CONFIG

random.seed(42)

CACHE_DIR = Path(__file__).parent / "cache"


def find_round_ids(start_year=2022):
    cal_re = re.compile(r"^(\d{4})-\d{2}-\d{2}_[a-f0-9]+\.json$")
    rounds = []
    for fp in sorted(glob.glob(str(CACHE_DIR / "*.json"))):
        fname = Path(fp).name
        m = cal_re.match(fname)
        if not m:
            continue
        if int(m.group(1)) < start_year:
            continue
        try:
            with open(fp) as f:
                data = json.load(f)
        except Exception:
            continue
        games = data.get("games", {})
        if not isinstance(games, dict):
            continue
        for gt in ("V75", "V86", "V85", "V64"):
            for g in games.get(gt, []):
                if g.get("status") == "results" and g.get("races"):
                    rounds.append({"game_type": gt, "game_id": g["id"]})
    return rounds


async def load_round(client, info):
    try:
        parts = info["game_id"].split("_")
        day = date.fromisoformat(parts[1])
        gr = await client.fetch_full_round(parts[0], day)
        if gr and gr.is_finished and gr.races:
            return gr
    except Exception:
        pass
    return None


def apply_temporal_filtering(gr):
    for race in gr.races:
        race_date = race.race_date
        for entry in race.entries:
            original = entry.horse.past_starts
            filtered = [s for s in original if s.start_date < race_date]
            if len(filtered) < len(original):
                entry.horse.past_starts = filtered
            entry.horse.recompute_career_from_starts()
            entry.bet_percentage = None
            entry.odds = None


async def main():
    t0 = time.time()
    client = ATGClient()
    analyzer = CompositeAnalyzer(DEFAULT_CONFIG)

    print("=" * 100)
    print("  GALOPPHISTORIK + VOLTSPÅR + TILLÄGG ANALYS")
    print("=" * 100)

    round_infos = find_round_ids()
    random.shuffle(round_infos)
    round_infos = round_infos[:350]

    # Load rounds
    all_races = []
    loaded = 0
    for info in round_infos:
        gr = await load_round(client, info)
        if not gr:
            continue
        has_results = all(r.result_order for r in gr.races)
        if not has_results:
            continue

        apply_temporal_filtering(gr)
        analyzer.analyze_round(gr)

        for race in gr.races:
            if race.result_order:
                all_races.append(race)

        loaded += 1
        if loaded % 25 == 0:
            print(f"  Loaded {loaded} rounds ({time.time() - t0:.0f}s)...")

    print(f"\nLoaded {loaded} rounds, {len(all_races)} races")

    # ═══════════════════════════════════════════════════════════════
    # 1. VOLTSPÅR: Vilka spår vinner i voltlopp?
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  1. VOLTSPÅR — Segerfrekvens per spår i voltlopp")
    print(f"{'═' * 100}")

    volt_races = [r for r in all_races if r.start_method == StartMethod.VOLT]
    auto_races = [r for r in all_races if r.start_method == StartMethod.AUTO]

    print(f"\n  Voltlopp: {len(volt_races)}, Autolopp: {len(auto_races)}")

    # Win rate by position in volt
    volt_wins = defaultdict(int)
    volt_top3 = defaultdict(int)
    volt_starters = defaultdict(int)
    volt_model_rank = defaultdict(list)  # Model rank of winner by post position

    for race in volt_races:
        winner_pp = race.result_order[0]
        top3_pps = race.result_order[:3]
        sorted_entries = sorted(race.active_entries, key=lambda e: e.super_score, reverse=True)

        for entry in race.active_entries:
            pp = entry.post_position
            volt_starters[pp] += 1
            if pp == winner_pp:
                volt_wins[pp] += 1
                # What was the model rank of this winner?
                rank = next((i+1 for i, e in enumerate(sorted_entries) if e.post_position == pp), 99)
                volt_model_rank[pp].append(rank)
            if pp in top3_pps:
                volt_top3[pp] += 1

    print(f"\n  {'Spår':>4} {'Starter':>7} {'Vinster':>7} {'Vinst%':>7} {'Top3':>5} {'Top3%':>6} {'AvgModelRank':>13}")
    print("  " + "─" * 55)
    for pp in sorted(volt_starters.keys()):
        if volt_starters[pp] < 20:
            continue
        win_pct = volt_wins[pp] / volt_starters[pp] * 100
        t3_pct = volt_top3[pp] / volt_starters[pp] * 100
        avg_rank = statistics.mean(volt_model_rank[pp]) if volt_model_rank[pp] else 0
        marker = " ★" if win_pct > 10 else ""
        print(f"  {pp:>4} {volt_starters[pp]:>7} {volt_wins[pp]:>7} {win_pct:>6.1f}% {volt_top3[pp]:>5} {t3_pct:>5.1f}% {avg_rank:>12.1f}{marker}")

    # Same for auto start
    print(f"\n  AUTOSTART — jämförelse:")
    auto_wins = defaultdict(int)
    auto_starters = defaultdict(int)
    for race in auto_races:
        winner_pp = race.result_order[0]
        for entry in race.active_entries:
            pp = entry.post_position
            auto_starters[pp] += 1
            if pp == winner_pp:
                auto_wins[pp] += 1

    print(f"  {'Spår':>4} {'Starter':>7} {'Vinster':>7} {'Vinst%':>7}")
    print("  " + "─" * 35)
    for pp in sorted(auto_starters.keys()):
        if auto_starters[pp] < 20:
            continue
        win_pct = auto_wins[pp] / auto_starters[pp] * 100
        print(f"  {pp:>4} {auto_starters[pp]:>7} {auto_wins[pp]:>7} {win_pct:>6.1f}%")

    # ═══════════════════════════════════════════════════════════════
    # 2. SPRINGSPÅR I VOLT (1, 6, 7)
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  2. SPRINGSPÅR I VOLT — Spår 1, 6, 7 vs resten")
    print(f"{'═' * 100}")

    # Group: springspår (1, 6, 7) vs innerspår (2-5) vs ytterspår (8+)
    groups = {
        "Springspår (1,6,7)": [1, 6, 7],
        "Innerspår (2-5)": [2, 3, 4, 5],
        "Ytterspår (8+)": list(range(8, 16)),
    }

    for label, positions in groups.items():
        starters = sum(volt_starters[pp] for pp in positions)
        wins = sum(volt_wins[pp] for pp in positions)
        t3 = sum(volt_top3[pp] for pp in positions)
        if starters == 0:
            continue
        print(f"  {label:<22} Starter: {starters:>5}  Vinster: {wins:>4} ({wins/starters*100:.1f}%)  Top3: {t3:>4} ({t3/starters*100:.1f}%)")

    # ═══════════════════════════════════════════════════════════════
    # 3. GALOPPHISTORIK — Påverkar det vinnarchansen?
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  3. GALOPPHISTORIK — Påverkar senaste galopperna utfallet?")
    print(f"{'═' * 100}")

    # For each entry: how many of last 5 starts had gallopp?
    gallop_analysis = {
        "galloped_last": {"wins": 0, "top3": 0, "total": 0},
        "galloped_2of5": {"wins": 0, "top3": 0, "total": 0},
        "galloped_3of5": {"wins": 0, "top3": 0, "total": 0},
        "no_gallop": {"wins": 0, "top3": 0, "total": 0},
    }

    # Favorite vs non-favorite with gallopp
    fav_gallop = {"wins": 0, "top3": 0, "total": 0}
    fav_no_gallop = {"wins": 0, "top3": 0, "total": 0}
    skrall_gallop = {"wins": 0, "top3": 0, "total": 0}
    skrall_no_gallop = {"wins": 0, "top3": 0, "total": 0}

    for race in all_races:
        winner_pp = race.result_order[0]
        top3_pps = set(race.result_order[:3])
        sorted_entries = sorted(race.active_entries, key=lambda e: e.super_score, reverse=True)

        for entry in race.active_entries:
            recent = entry.horse.recent_starts(5)
            if not recent:
                continue

            is_winner = entry.post_position == winner_pp
            is_top3 = entry.post_position in top3_pps
            model_rank = next((i+1 for i, e in enumerate(sorted_entries) if e.post_position == entry.post_position), 99)
            is_favorite = model_rank <= 2
            is_skrall = model_rank >= 6

            galloped_last = recent[0].galloped if recent else False
            gallop_count_5 = sum(1 for s in recent[:5] if s.galloped)

            # Gallopp history categories
            if galloped_last:
                gallop_analysis["galloped_last"]["total"] += 1
                if is_winner: gallop_analysis["galloped_last"]["wins"] += 1
                if is_top3: gallop_analysis["galloped_last"]["top3"] += 1
            elif gallop_count_5 >= 3:
                gallop_analysis["galloped_3of5"]["total"] += 1
                if is_winner: gallop_analysis["galloped_3of5"]["wins"] += 1
                if is_top3: gallop_analysis["galloped_3of5"]["top3"] += 1
            elif gallop_count_5 >= 2:
                gallop_analysis["galloped_2of5"]["total"] += 1
                if is_winner: gallop_analysis["galloped_2of5"]["wins"] += 1
                if is_top3: gallop_analysis["galloped_2of5"]["top3"] += 1
            else:
                gallop_analysis["no_gallop"]["total"] += 1
                if is_winner: gallop_analysis["no_gallop"]["wins"] += 1
                if is_top3: gallop_analysis["no_gallop"]["top3"] += 1

            # Favorite + gallopp interaction
            if is_favorite:
                if gallop_count_5 >= 1:
                    fav_gallop["total"] += 1
                    if is_winner: fav_gallop["wins"] += 1
                    if is_top3: fav_gallop["top3"] += 1
                else:
                    fav_no_gallop["total"] += 1
                    if is_winner: fav_no_gallop["wins"] += 1
                    if is_top3: fav_no_gallop["top3"] += 1

            if is_skrall:
                if gallop_count_5 >= 1:
                    skrall_gallop["total"] += 1
                    if is_winner: skrall_gallop["wins"] += 1
                    if is_top3: skrall_gallop["top3"] += 1
                else:
                    skrall_no_gallop["total"] += 1
                    if is_winner: skrall_no_gallop["wins"] += 1
                    if is_top3: skrall_no_gallop["top3"] += 1

    print(f"\n  {'Galopphistorik':<22} {'Total':>6} {'Vinster':>7} {'Vinst%':>7} {'Top3':>5} {'Top3%':>6}")
    print("  " + "─" * 55)
    for label, data in gallop_analysis.items():
        if data["total"] == 0:
            continue
        print(f"  {label:<22} {data['total']:>6} {data['wins']:>7} {data['wins']/data['total']*100:>6.1f}% {data['top3']:>5} {data['top3']/data['total']*100:>5.1f}%")

    print(f"\n  FAVORIT (modellrank 1-2) med/utan galopphistorik:")
    print(f"  {'Kategori':<28} {'Total':>6} {'Vinster':>7} {'Vinst%':>7} {'Top3':>5} {'Top3%':>6}")
    print("  " + "─" * 60)
    for label, data in [
        ("Favorit + galopp", fav_gallop),
        ("Favorit utan galopp", fav_no_gallop),
        ("Skräll + galopp (rank 6+)", skrall_gallop),
        ("Skräll utan galopp", skrall_no_gallop),
    ]:
        if data["total"] == 0:
            continue
        print(f"  {label:<28} {data['total']:>6} {data['wins']:>7} {data['wins']/data['total']*100:>6.1f}% {data['top3']:>5} {data['top3']/data['total']*100:>5.1f}%")

    # ═══════════════════════════════════════════════════════════════
    # 4. GALLOPP + VOLTSPÅR INTERAKTION
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  4. GALLOPP-RISK PER VOLTSPÅR")
    print("  Dennis: 'spår 2 och 4 i volt ökar galopprisken pga trångt'")
    print(f"{'═' * 100}")

    # Count gallopp outcomes by post position in volt races
    # We look at CURRENT race gallopp (from result) — did the horse gallop in THIS race?
    # Problem: we only have gallop in past_starts, not current race
    # Alternative: look at disqualifications in current race
    # But result_order might not include DQ'd horses

    # Let's look at it from the OTHER side: horses that gallop tend to come from which positions?
    # We can check past_starts where start_method == VOLT and galloped == True
    volt_gallop_by_pos = defaultdict(lambda: {"galloped": 0, "total": 0})

    for race in all_races:
        for entry in race.active_entries:
            for start in entry.horse.past_starts:
                if start.start_method == StartMethod.VOLT and start.post_position > 0:
                    pp = start.post_position
                    volt_gallop_by_pos[pp]["total"] += 1
                    if start.galloped:
                        volt_gallop_by_pos[pp]["galloped"] += 1

    print(f"\n  Galoppfrekvens per spår i voltlopp (historisk data):")
    print(f"  {'Spår':>4} {'Total':>7} {'Galopp':>7} {'Galopp%':>8}")
    print("  " + "─" * 35)
    for pp in sorted(volt_gallop_by_pos.keys()):
        data = volt_gallop_by_pos[pp]
        if data["total"] < 100:
            continue
        gal_pct = data["galloped"] / data["total"] * 100
        marker = " ⚠" if gal_pct > 6 else ""
        print(f"  {pp:>4} {data['total']:>7} {data['galloped']:>7} {gal_pct:>7.1f}%{marker}")

    # ═══════════════════════════════════════════════════════════════
    # 5. TILLÄGG — Springspår i andra volten
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  5. TILLÄGG — Position i andra volten vs segerfrekvens")
    print("  Dennis: 'häst nr 13 kan ha springspår i andra volten'")
    print(f"{'═' * 100}")

    # For tillägg races: calculate "effective volt position" for horses on tillägg
    tillagg_analysis = defaultdict(lambda: {"wins": 0, "total": 0, "top3": 0})

    for race in volt_races:
        # Check if race has tillägg
        entries_with_tillagg = [
            e for e in race.active_entries
            if e.distance > 0 and race.distance > 0 and e.distance > race.distance
        ]
        if not entries_with_tillagg:
            continue

        winner_pp = race.result_order[0]
        top3_pps = set(race.result_order[:3])

        # First volt: positions 1-N (depends on field size)
        # Tillägg horses are in the second volt
        n_first_volt = len([e for e in race.active_entries if e.distance == race.distance])

        for entry in entries_with_tillagg:
            tillagg_m = entry.distance - race.distance
            pp = entry.post_position
            # Position in second volt (approximate)
            second_volt_pos = pp - n_first_volt
            if second_volt_pos <= 0:
                second_volt_pos = pp  # Fallback

            key = f"tillagg_{tillagg_m}m"
            tillagg_analysis[key]["total"] += 1
            if entry.post_position == winner_pp:
                tillagg_analysis[key]["wins"] += 1
            if entry.post_position in top3_pps:
                tillagg_analysis[key]["top3"] += 1

            # Also by position in second volt
            key2 = f"volt2_pos_{second_volt_pos}"
            tillagg_analysis[key2]["total"] += 1
            if entry.post_position == winner_pp:
                tillagg_analysis[key2]["wins"] += 1
            if entry.post_position in top3_pps:
                tillagg_analysis[key2]["top3"] += 1

    print(f"\n  {'Kategori':<20} {'Total':>6} {'Vinster':>7} {'Vinst%':>7} {'Top3':>5} {'Top3%':>6}")
    print("  " + "─" * 50)
    # Print tillägg categories first
    for key in sorted(tillagg_analysis.keys()):
        if not key.startswith("tillagg_"):
            continue
        data = tillagg_analysis[key]
        if data["total"] < 10:
            continue
        win_pct = data["wins"] / data["total"] * 100
        t3_pct = data["top3"] / data["total"] * 100
        print(f"  {key:<20} {data['total']:>6} {data['wins']:>7} {win_pct:>6.1f}% {data['top3']:>5} {t3_pct:>5.1f}%")

    print(f"\n  Position i andra volten:")
    for key in sorted(tillagg_analysis.keys()):
        if not key.startswith("volt2_pos_"):
            continue
        data = tillagg_analysis[key]
        if data["total"] < 10:
            continue
        win_pct = data["wins"] / data["total"] * 100
        t3_pct = data["top3"] / data["total"] * 100
        pos = key.split("_")[-1]
        marker = " ★" if win_pct > 10 else ""
        print(f"  Position {pos:<8} {data['total']:>6} {data['wins']:>7} {win_pct:>6.1f}% {data['top3']:>5} {t3_pct:>5.1f}%{marker}")

    # ═══════════════════════════════════════════════════════════════
    # 6. SAMMANSATT: Galopphistorik → Skrällrisk?
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  6. GALOPPHISTORIK → EFFEKT PÅ SKRÄLLFREKVENS PER LOPP")
    print(f"{'═' * 100}")

    # Per race: does the presence of a gallopping favorite increase upset frequency?
    fav_galloped_races = {"upsets": 0, "total": 0}
    fav_clean_races = {"upsets": 0, "total": 0}

    for race in all_races:
        winner_pp = race.result_order[0]
        sorted_entries = sorted(race.active_entries, key=lambda e: e.super_score, reverse=True)
        if not sorted_entries:
            continue

        # Model favorite
        favorite = sorted_entries[0]
        fav_won = favorite.post_position == winner_pp

        # Check if favorite has gallopp history
        recent = favorite.horse.recent_starts(5)
        fav_has_gallop = any(s.galloped for s in recent) if recent else False

        # Is winner a big upset? (rank 5+ in model)
        winner_rank = next((i+1 for i, e in enumerate(sorted_entries) if e.post_position == winner_pp), 99)
        is_upset = winner_rank >= 5

        if fav_has_gallop:
            fav_galloped_races["total"] += 1
            if is_upset:
                fav_galloped_races["upsets"] += 1
        else:
            fav_clean_races["total"] += 1
            if is_upset:
                fav_clean_races["upsets"] += 1

    print(f"\n  Lopp där modellens favorit HAR galopphistorik:")
    print(f"    Antal lopp: {fav_galloped_races['total']}")
    if fav_galloped_races["total"] > 0:
        print(f"    Skrällar (vinnare rank 5+): {fav_galloped_races['upsets']} ({fav_galloped_races['upsets']/fav_galloped_races['total']*100:.1f}%)")

    print(f"\n  Lopp där modellens favorit INTE har galopphistorik:")
    print(f"    Antal lopp: {fav_clean_races['total']}")
    if fav_clean_races["total"] > 0:
        print(f"    Skrällar (vinnare rank 5+): {fav_clean_races['upsets']} ({fav_clean_races['upsets']/fav_clean_races['total']*100:.1f}%)")

    # ═══════════════════════════════════════════════════════════════
    # 7. IMPACT PÅ SYSTEM: Om vi integrerar dessa signaler
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  7. SAMMANFATTNING — Signaler att integrera i modellen")
    print(f"{'═' * 100}")

    print("""
  VOLTSPÅR:
  - Analysdata visar segerfrekvens per spår i volt
  - Springspår (1, 6, 7) vs innerspår (2-5) vs ytterspår (8+)
  - Används för att justera post_position-faktorn i voltlopp

  GALOPPHISTORIK:
  - Favoriter med galopphistorik → ökad skrällrisk i loppet
  - Skrällar med galopphistorik → oförutsägbar, kan överraska
  - Bör påverka predict_difficulty() och upset_risk

  TILLÄGG-POSITION:
  - Position i andra volten påverkar chanserna
  - Springspår i andra volten = extra fördel
  - Bör integreras i competition_strength och post_position
    """)

    elapsed = time.time() - t0
    print(f"\nTotal runtime: {elapsed:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
