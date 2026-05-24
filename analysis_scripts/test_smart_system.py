#!/usr/bin/env python3
"""Backtest the smart variable-width system builder vs fixed-width.

Tests both approaches on historical V75/V85 rounds with temporal filtering.
"""

import asyncio
import json
import glob
import re
import sys
import random
import time
from datetime import date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.data.models import GameRound, Race, StartMethod
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.analysis.system_builder import (
    build_system, predict_difficulty,
)
from trav_agent.config import DEFAULT_CONFIG

random.seed(42)

CACHE_DIR = Path(__file__).parent / "cache"
ROW_PRICE = 0.50


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
        for gt in ("V75", "V85"):
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


def check_system_hits(gr, plan):
    """Check how many legs the system covers correctly."""
    correct = 0
    total = len(gr.races)

    for race in gr.races:
        if not race.result_order:
            continue
        winner_pp = race.result_order[0]
        leg = next((l for l in plan.legs if l.race_number == race.race_number), None)
        if leg and winner_pp in leg.picks[:leg.num_picks]:
            correct += 1

    return correct, total


def fixed_width_system(gr, n_spikes, rest_width, budget):
    """Simple fixed-width system: N spikes (lowest risk) + rest_width others."""
    # Sort races by upset_risk
    race_risks = []
    for race in gr.races:
        entries = sorted(race.active_entries, key=lambda e: e.super_score, reverse=True)
        risk = race.upset_risk
        race_risks.append((race, risk, entries))

    race_risks.sort(key=lambda x: x[1])

    # Assign picks
    picks_per_race = {}
    for i, (race, risk, entries) in enumerate(race_risks):
        if i < n_spikes:
            picks_per_race[race.race_number] = [e.post_position for e in entries[:1]]
        else:
            w = min(rest_width, len(entries))
            picks_per_race[race.race_number] = [e.post_position for e in entries[:w]]

    # Budget constraint
    rows = 1
    for pp_list in picks_per_race.values():
        rows *= len(pp_list)
    cost = rows * ROW_PRICE

    while cost > budget:
        # Reduce widest non-spike
        widest_rn = max(
            (rn for rn, pp in picks_per_race.items() if len(pp) > 1),
            key=lambda rn: len(picks_per_race[rn]),
            default=None,
        )
        if widest_rn is None or len(picks_per_race[widest_rn]) <= 1:
            break
        picks_per_race[widest_rn] = picks_per_race[widest_rn][:-1]
        rows = 1
        for pp_list in picks_per_race.values():
            rows *= len(pp_list)
        cost = rows * ROW_PRICE

    # Check hits
    correct = 0
    total = len(gr.races)
    for race in gr.races:
        if not race.result_order:
            continue
        winner_pp = race.result_order[0]
        if winner_pp in picks_per_race.get(race.race_number, []):
            correct += 1

    return correct, total, cost


async def main():
    t0 = time.time()
    client = ATGClient()
    analyzer = CompositeAnalyzer(DEFAULT_CONFIG)

    print("=" * 90)
    print("  SMART VARIABLE vs FIXED WIDTH SYSTEM BACKTEST")
    print("=" * 90)

    round_infos = find_round_ids()
    random.shuffle(round_infos)
    round_infos = round_infos[:200]

    # Load rounds
    rounds_data = []
    loaded = 0
    for info in round_infos:
        gr = await load_round(client, info)
        if not gr:
            continue
        has_results = all(r.result_order for r in gr.races)
        if not has_results:
            continue
        num_legs = len(gr.races)
        top_div = gr.dividends.get(num_legs, 0.0)
        n1_div = gr.dividends.get(num_legs - 1, 0.0)
        if top_div <= 0:
            continue

        apply_temporal_filtering(gr)
        analyzer.analyze_round(gr)
        rounds_data.append((gr, top_div, n1_div))
        loaded += 1
        if loaded % 20 == 0:
            print(f"  Loaded {loaded} rounds ({time.time() - t0:.0f}s)...")

    print(f"\nLoaded {loaded} rounds for testing")

    # ── Test strategies ──────────────────────────────────────────

    budgets = [200, 500, 1000]

    for budget in budgets:
        print(f"\n{'═' * 90}")
        print(f"  BUDGET: {budget} kr")
        print(f"{'═' * 90}")

        strategies = {}

        # Smart variable
        strategies["smart_variable"] = {"hits": 0, "n1": 0, "n2": 0,
                                         "total_cost": 0, "total_payout": 0,
                                         "tested": 0, "all_picks": []}

        # Fixed width baselines
        for n_spikes in [2, 3, 4]:
            for rest_w in [3, 4, 5, 6]:
                name = f"fixed_{n_spikes}S+rest{rest_w}"
                strategies[name] = {"hits": 0, "n1": 0, "n2": 0,
                                     "total_cost": 0, "total_payout": 0,
                                     "tested": 0, "all_picks": []}

        for gr, dividend, n1_div in rounds_data:
            num_legs = len(gr.races)

            # Smart variable
            plan = build_system(gr, budget=budget, strategy="optimal")
            correct, total = check_system_hits(gr, plan)
            s = strategies["smart_variable"]
            s["tested"] += 1
            s["total_cost"] += plan.total_cost
            s["all_picks"].extend([l.num_picks for l in plan.legs])
            if correct == total:
                s["hits"] += 1
                s["total_payout"] += dividend
            elif correct == total - 1:
                s["n1"] += 1
            elif correct == total - 2:
                s["n2"] += 1

            # Fixed width baselines
            for n_spikes in [2, 3, 4]:
                for rest_w in [3, 4, 5, 6]:
                    name = f"fixed_{n_spikes}S+rest{rest_w}"
                    correct, total, cost = fixed_width_system(
                        gr, n_spikes, rest_w, budget
                    )
                    s = strategies[name]
                    s["tested"] += 1
                    s["total_cost"] += cost
                    if correct == total:
                        s["hits"] += 1
                        s["total_payout"] += dividend
                    elif correct == total - 1:
                        s["n1"] += 1
                    elif correct == total - 2:
                        s["n2"] += 1

        # Print results
        print(f"\n  {'Strategy':<25} {'Tested':>6} {'Hits':>4} {'N-1':>4} {'N-2':>4} "
              f"{'HitRate':>7} {'AvgCost':>8} {'ROI':>8} {'AvgPicks':>9}")
        print("  " + "-" * 80)

        # Sort by hits descending, then ROI
        sorted_strats = sorted(
            strategies.items(),
            key=lambda x: (x[1]["hits"], x[1]["total_payout"] - x[1]["total_cost"]),
            reverse=True,
        )

        for name, s in sorted_strats:
            if s["tested"] == 0:
                continue
            hitrate = s["hits"] / s["tested"]
            avg_cost = s["total_cost"] / s["tested"]
            roi = (s["total_payout"] - s["total_cost"]) / s["total_cost"] if s["total_cost"] > 0 else 0
            avg_picks = sum(s["all_picks"]) / len(s["all_picks"]) if s["all_picks"] else 0

            marker = " ◀◀◀" if name == "smart_variable" else ""
            print(f"  {name:<25} {s['tested']:>6} {s['hits']:>4} {s['n1']:>4} {s['n2']:>4} "
                  f"{hitrate:>6.1%} {avg_cost:>7.0f}kr {roi:>+7.0%} {avg_picks:>8.1f}{marker}")

    # ── Print difficulty distribution for the smart variable system ──
    print(f"\n\n{'═' * 90}")
    print("  PICKS DISTRIBUTION (Smart Variable, budget 500kr)")
    print(f"{'═' * 90}")

    pick_counts = defaultdict(int)
    diff_by_picks = defaultdict(list)

    for gr, _, _ in rounds_data:
        plan = build_system(gr, budget=500, strategy="optimal")
        for leg in plan.legs:
            pick_counts[leg.num_picks] += 1
            diff_by_picks[leg.num_picks].append(leg.difficulty)

    print(f"\n  {'Picks':>5} {'Count':>6} {'%':>6} {'AvgDiff':>8} {'MinDiff':>8} {'MaxDiff':>8}")
    print("  " + "-" * 45)
    total_legs = sum(pick_counts.values())
    for n_picks in sorted(pick_counts.keys()):
        count = pick_counts[n_picks]
        diffs = diff_by_picks[n_picks]
        avg_d = sum(diffs) / len(diffs)
        min_d = min(diffs)
        max_d = max(diffs)
        print(f"  {n_picks:>5} {count:>6} {count/total_legs:>5.1%} {avg_d:>7.1f} {min_d:>7.1f} {max_d:>7.1f}")

    elapsed = time.time() - t0
    print(f"\nTotal runtime: {elapsed:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
