#!/usr/bin/env python3
"""
Live scoring weight backtest — kör NUVARANDE modellen mot historiska omgångar.

Till skillnad från weight_backtest_large.py som använder gamla sparade poäng,
kör detta script CompositeAnalyzer live mot ATG API:t och mäter vilken
viktning mellan composite_score och streckprocent som ger bäst ranking.

Kör ~30 omgångar bakåt (alla speltyper) för att få ~200+ lopp.
"""

import asyncio
import math
import sys
import time
from datetime import date, timedelta

# Add parent to path
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import AnalysisConfig


WEIGHTS = [0.0, 0.10, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.75, 0.80, 0.90, 1.0]

# Game schedules (day of week → game type)
# V85 = Saturday (5), V86 = Wednesday (2), V64 = Mon/Tue/Thu/Fri, GS75 = Sunday (6)
GAME_SCHEDULE = [
    ("V64", 0),   # Monday
    ("V64", 1),   # Tuesday
    ("V86", 2),   # Wednesday
    ("V64", 3),   # Thursday
    ("V64", 4),   # Friday
    ("V85", 5),   # Saturday
    ("GS75", 6),  # Sunday
]


async def fetch_and_analyze(client, analyzer, game_type, day):
    """Fetch a round and analyze it. Returns list of race data dicts."""
    try:
        game_round = await client.fetch_full_round(game_type, day)
    except Exception:
        return None
    if not game_round or not game_round.races:
        return None

    races = []
    for race in game_round.races:
        try:
            analyzer.analyze_race(race)
        except Exception:
            continue

        entries = race.active_entries
        if not entries:
            continue

        # Get actual result
        result_order = race.result_order
        if not result_order:
            continue
        winner = result_order[0]

        # Check winner is in entries
        winner_entry = None
        for e in entries:
            if e.post_position == winner:
                winner_entry = e
                break
        if not winner_entry:
            continue

        races.append({
            "race_number": race.race_number,
            "winner": winner,
            "entries": [
                {
                    "post": e.post_position,
                    "comp": e.composite_score,
                    "streck": e.bet_percentage or 0,
                }
                for e in entries
            ],
        })

    return races


def rank_by_blend(entries, model_weight):
    """Rank entries by blended score."""
    max_comp = max(e["comp"] for e in entries) if entries else 1
    min_comp = min(e["comp"] for e in entries) if entries else 0
    comp_range = max_comp - min_comp if max_comp > min_comp else 1
    max_streck = max(e["streck"] for e in entries) if entries else 1

    scored = []
    for e in entries:
        comp_norm = (e["comp"] - min_comp) / comp_range
        streck_norm = e["streck"] / max_streck if max_streck > 0 else 0
        blend = model_weight * comp_norm + (1 - model_weight) * streck_norm
        scored.append((e["post"], blend))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [s[0] for s in scored]


def evaluate_weight(all_races, weight):
    """Evaluate a weight across all races."""
    r1 = top3 = top5 = 0
    n = 0
    for race in all_races:
        ranking = rank_by_blend(race["entries"], weight)
        if not ranking:
            continue
        n += 1
        winner = race["winner"]
        if ranking[0] == winner:
            r1 += 1
        if winner in ranking[:3]:
            top3 += 1
        if winner in ranking[:5]:
            top5 += 1
    return r1, top3, top5, n


async def main():
    client = ATGClient()
    config = AnalysisConfig()
    analyzer = CompositeAnalyzer(config)

    # Go back N weeks from today
    today = date.today()
    weeks_back = 8  # ~56 days, should give ~40-50 rounds

    all_races = []
    rounds_ok = 0
    rounds_skip = 0

    print(f"Kör nuvarande modell mot {weeks_back} veckors historik...", flush=True)
    print(flush=True)

    current = today - timedelta(days=1)  # Start yesterday
    end_date = today - timedelta(weeks=weeks_back)

    while current >= end_date:
        weekday = current.weekday()
        game_type = None
        for gt, wd in GAME_SCHEDULE:
            if wd == weekday:
                game_type = gt
                break

        if game_type:
            races = await fetch_and_analyze(client, analyzer, game_type, current)
            if races:
                rounds_ok += 1
                all_races.extend(races)
                print(f"  {game_type} {current}: {len(races)} lopp", flush=True)
            else:
                rounds_skip += 1

        current -= timedelta(days=1)

    print(flush=True)
    if not all_races:
        print("Inga lopp hittade!")
        return

    # Correlation check
    comps = []
    bets = []
    same_top1 = 0
    for race in all_races:
        entries = race["entries"]
        top_comp = max(entries, key=lambda e: e["comp"])
        top_streck = max(entries, key=lambda e: e["streck"])
        if top_comp["post"] == top_streck["post"]:
            same_top1 += 1
        for e in entries:
            if e["streck"] > 0:
                comps.append(e["comp"])
                bets.append(e["streck"] * 100)

    n = len(comps)
    mean_c = sum(comps) / n
    mean_b = sum(bets) / n
    cov = sum((c - mean_c) * (b - mean_b) for c, b in zip(comps, bets)) / n
    std_c = math.sqrt(sum((c - mean_c) ** 2 for c in comps) / n)
    std_b = math.sqrt(sum((b - mean_b) ** 2 for b in bets) / n)
    corr = cov / (std_c * std_b) if std_c > 0 and std_b > 0 else 0

    total_races = len(all_races)
    print("=" * 75, flush=True)
    print(f"RESULTAT: {rounds_ok} omgångar, {total_races} lopp (skip: {rounds_skip})", flush=True)
    print(f"Korrelation comp vs streck: {corr:.3f}", flush=True)
    print(f"Samma #1 (comp vs streck): {same_top1}/{total_races} = {same_top1/total_races*100:.0f}%", flush=True)
    print("=" * 75, flush=True)
    print(flush=True)

    print(f"{'Viktning':<30} {'Rank1%':>8} {'Top3%':>8} {'Top5%':>8} {'Lopp':>6}", flush=True)
    print(f"{'-'*30} {'-'*8} {'-'*8} {'-'*8} {'-'*6}", flush=True)

    # Pure market baseline
    r1, t3, t5, n = evaluate_weight(all_races, 0.0)
    print(f"{'Ren marknad (streck)':<30} {r1/n*100:>7.1f}% {t3/n*100:>7.1f}% {t5/n*100:>7.1f}% {n:>6}", flush=True)

    best_r1 = ("", 0)
    best_t3 = ("", 0)
    best_t5 = ("", 0)
    results = {}

    for w in WEIGHTS:
        label = f"Modell {int(w*100)}% + Mark {int((1-w)*100)}%"
        if w == 0.25:
            label += " (nu)"
        r1, t3, t5, n = evaluate_weight(all_races, w)
        r1p = r1 / n * 100
        t3p = t3 / n * 100
        t5p = t5 / n * 100
        results[w] = (r1p, t3p, t5p, n)
        print(f"{label:<30} {r1p:>7.1f}% {t3p:>7.1f}% {t5p:>7.1f}% {n:>6}", flush=True)

        if r1p > best_r1[1]:
            best_r1 = (label, r1p)
        if t3p > best_t3[1]:
            best_t3 = (label, t3p)
        if t5p > best_t5[1]:
            best_t5 = (label, t5p)

    print(flush=True)
    print(f"Bäst Rank-1: {best_r1[0]} ({best_r1[1]:.1f}%)", flush=True)
    print(f"Bäst Top-3:  {best_t3[0]} ({best_t3[1]:.1f}%)", flush=True)
    print(f"Bäst Top-5:  {best_t5[0]} ({best_t5[1]:.1f}%)", flush=True)

    # Delta vs market
    baseline = results[0.0]
    print(flush=True)
    print(f"{'Viktning':<30} {'ΔRank1':>8} {'ΔTop3':>8} {'ΔTop5':>8}", flush=True)
    print(f"{'-'*30} {'-'*8} {'-'*8} {'-'*8}", flush=True)
    for w in WEIGHTS:
        if w == 0.0:
            continue
        label = f"Modell {int(w*100)}%"
        if w == 0.25:
            label += " (nu)"
        r = results[w]
        d1 = r[0] - baseline[0]
        d3 = r[1] - baseline[1]
        d5 = r[2] - baseline[2]
        s1 = "+" if d1 >= 0 else ""
        s3 = "+" if d3 >= 0 else ""
        s5 = "+" if d5 >= 0 else ""
        print(f"{label:<30} {s1}{d1:>6.1f}pp {s3}{d3:>6.1f}pp {s5}{d5:>6.1f}pp", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
