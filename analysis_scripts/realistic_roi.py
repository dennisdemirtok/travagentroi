#!/usr/bin/env python3
"""Realistic ROI calculator for spike_easiest_rest4 strategy.

Uses ACTUAL historical payouts from game cache files instead of estimates.
Compares 3 variants:
  - Pure model (model_weight=1.0)
  - Hybrid 20/80 (model_weight=0.20)
  - Pure market (model_weight=0.0)

Strategy: spike_easiest_rest4
  - Find the "easiest" race (biggest favorite) -> 1 pick (spik)
  - Remaining races -> top 4 picks each
  - Cost = row_price * 1 * 4^(n-1)
  - Row prices: V75=0.50, V85=0.50, V86=0.25
"""

import json
import glob
import sys
from collections import defaultdict
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════

ROW_PRICES = {"V75": 0.50, "V85": 0.50, "V86": 0.25}
N_CORRECT_KEY = {"V75": "7", "V85": "8", "V86": "8"}  # key in result.payouts
CACHE_DIR = Path(__file__).parent / "cache"
BACKTEST_DIR = Path(__file__).parent / "backtest_results"


# ═══════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════

def load_cache_file(game_type, date_str):
    """Load the game cache file for a given game type and date."""
    pattern = str(CACHE_DIR / f"{game_type}_{date_str}_*.json")
    files = glob.glob(pattern)
    if not files:
        return None
    with open(files[0]) as f:
        return json.load(f)


def load_backtest_results():
    """Load all backtest predictions."""
    all_rounds = []
    for gt in ["V75", "V85", "V86"]:
        bt_file = BACKTEST_DIR / f"backtest_{gt}_2024-01-01_2026-04-01.json"
        if not bt_file.exists():
            continue
        with open(bt_file) as f:
            data = json.load(f)
        game_type = data["game_type"]
        for rd in data["predictions"]:
            date_str = rd["date"]
            races = []
            for leg_idx, race in enumerate(rd["races"]):
                actual = race.get("actual_result", [])
                ranking = race.get("predicted_ranking", [])
                scores = race.get("horse_scores", {})
                if not actual or not ranking:
                    continue
                races.append({
                    "leg_idx": leg_idx,
                    "race_number": race.get("race_number", 0),
                    "track": race.get("track", "?"),
                    "actual": actual,
                    "model_ranking": ranking,
                    "model_scores": {int(k): v for k, v in scores.items()},
                    "winner": actual[0],
                })
            if races:
                all_rounds.append({
                    "game_type": game_type,
                    "date": date_str,
                    "races": races,
                })
    return all_rounds


def enrich_with_cache(rounds):
    """Add market data (betDistribution) and actual payout data from cache."""
    stats = {"matched": 0, "no_cache": 0, "no_payout": 0}
    for rd in rounds:
        gt = rd["game_type"]
        date_str = rd["date"]
        cache = load_cache_file(gt, date_str)
        if not cache:
            stats["no_cache"] += 1
            rd["actual_payout_per_row"] = None
            rd["turnover"] = None
            rd["jackpot"] = False
            continue

        stats["matched"] += 1

        # Extract pool-level data
        pool = cache.get("pools", {}).get(gt, {})
        rd["turnover"] = pool.get("turnover", 0)

        # Extract actual payout for full-correct
        n_key = N_CORRECT_KEY[gt]
        result = pool.get("result", {})
        result_payouts = result.get("payouts", {})
        payout_info = result_payouts.get(n_key, {})

        if payout_info.get("jackpot", False) or "payout" not in payout_info:
            rd["actual_payout_per_row"] = None
            rd["jackpot"] = True
            stats["no_payout"] += 1
        else:
            # payout is in öre (1/100 kr)
            rd["actual_payout_per_row"] = payout_info["payout"] / 100.0
            rd["jackpot"] = False
            rd["actual_n_winners"] = payout_info.get("systems", 0)

        # Enrich each race with bet distribution from cache
        cache_races = cache.get("races", [])
        for race in rd["races"]:
            leg_idx = race["leg_idx"]
            if leg_idx < len(cache_races):
                cache_race = cache_races[leg_idx]
                bet_dist = {}
                for s in cache_race.get("starts", []):
                    num = s.get("number", 0)
                    vpool = s.get("pools", {}).get(gt, {})
                    bd = vpool.get("betDistribution", 0)
                    if bd > 0:
                        bet_dist[num] = bd / 100.0  # basis points -> percentage
                race["bet_dist"] = bet_dist
            else:
                race["bet_dist"] = {}

    return stats


# ═══════════════════════════════════════════════════════════════
# HYBRID RANKING
# ═══════════════════════════════════════════════════════════════

def compute_ranking(race, model_weight):
    """Compute ranking by blending model scores and market bet distribution."""
    model_scores = race["model_scores"]
    bet_dist = race.get("bet_dist", {})

    if model_weight >= 1.0 or not bet_dist:
        return sorted(model_scores.keys(),
                      key=lambda h: model_scores.get(h, 0), reverse=True)

    if model_weight <= 0.0:
        return sorted(bet_dist.keys(),
                      key=lambda h: bet_dist.get(h, 0), reverse=True)

    # Hybrid
    common = set(model_scores.keys()) & set(bet_dist.keys())
    if len(common) < 3:
        return sorted(model_scores.keys(),
                      key=lambda h: model_scores.get(h, 0), reverse=True)

    bd_vals = [bet_dist[h] for h in common]
    ms_vals = [model_scores[h] for h in common]
    bd_min, bd_max = min(bd_vals), max(bd_vals)
    ms_min, ms_max = min(ms_vals), max(ms_vals)
    bd_spread = bd_max - bd_min if bd_max > bd_min else 1.0
    ms_spread = ms_max - ms_min if ms_max > ms_min else 1.0

    hybrid = {}
    for h in common:
        m_norm = (model_scores[h] - ms_min) / ms_spread * 100
        k_norm = (bet_dist[h] - bd_min) / bd_spread * 100
        hybrid[h] = model_weight * m_norm + (1 - model_weight) * k_norm

    return sorted(common, key=lambda h: hybrid[h], reverse=True)


# ═══════════════════════════════════════════════════════════════
# SYSTEM BUILDER (spike_easiest_rest4)
# ═══════════════════════════════════════════════════════════════

def build_spike_easiest_rest4(round_data, model_weight):
    """Build spike_easiest_rest4 system for one round.

    Returns dict with picks, cost, whether system won, and payout if won.
    """
    gt = round_data["game_type"]
    races = round_data["races"]
    n_races = len(races)

    expected_n = 7 if gt == "V75" else 8
    if n_races < expected_n:
        return None

    row_price = ROW_PRICES[gt]

    # Rank each race and compute easiness
    race_info = []
    for race in races:
        ranking = compute_ranking(race, model_weight)
        winner = race["winner"]
        bd = race.get("bet_dist", {})

        # Easiness: how dominant is the top pick?
        if len(ranking) >= 2 and bd:
            top1_streck = bd.get(ranking[0], 0)
            top2_streck = bd.get(ranking[1], 0)
            gap = top1_streck - top2_streck
            easiness = top1_streck + gap
        else:
            scores = race["model_scores"]
            vals = sorted(scores.values(), reverse=True)
            easiness = (vals[0] - vals[1]) if len(vals) >= 2 else 0

        race_info.append({
            "ranking": ranking,
            "winner": winner,
            "easiness": easiness,
        })

    # Find easiest race -> spike (1 pick), rest -> 4 picks
    sorted_by_ease = sorted(range(n_races),
                            key=lambda i: race_info[i]["easiness"],
                            reverse=True)
    spike_idx = sorted_by_ease[0]

    picks_per_race = []
    for i in range(n_races):
        ranking = race_info[i]["ranking"]
        if i == spike_idx:
            picks_per_race.append(ranking[:1])
        else:
            picks_per_race.append(ranking[:4])

    # Cost = row_price * product(picks per race)
    n_rows = 1
    for picks in picks_per_race:
        n_rows *= len(picks)
    cost = row_price * n_rows  # = row_price * 1 * 4^(n-1)

    # Check if system won (winner in picks for ALL races)
    all_correct = True
    for i, picks in enumerate(picks_per_race):
        if race_info[i]["winner"] not in picks:
            all_correct = False
            break

    # Determine payout
    payout = 0.0
    if all_correct:
        actual_payout = round_data.get("actual_payout_per_row")
        if actual_payout is not None:
            payout = actual_payout  # kr per winning row
        else:
            # Jackpot round or missing data: estimate from winner bet distributions
            payout = estimate_payout_from_streck(round_data, races)

    spike_correct = race_info[spike_idx]["winner"] in picks_per_race[spike_idx]

    return {
        "cost": cost,
        "n_rows": n_rows,
        "won": all_correct,
        "payout": payout,
        "spike_idx": spike_idx,
        "spike_horse": picks_per_race[spike_idx][0],
        "spike_correct": spike_correct,
        "picks_per_race": picks_per_race,
        "jackpot_round": round_data.get("jackpot", False),
        "actual_payout_used": round_data.get("actual_payout_per_row") is not None if all_correct else False,
    }


def estimate_payout_from_streck(round_data, races):
    """Fallback: estimate payout from winner bet distributions and turnover."""
    gt = round_data["game_type"]
    turnover = round_data.get("turnover", 0)
    if not turnover:
        # Use typical pool sizes
        typical = {"V75": 10_000_000_000, "V85": 8_000_000_000, "V86": 2_500_000_000}
        turnover = typical.get(gt, 5_000_000_000)

    # turnover is in öre, convert to kr
    turnover_kr = turnover / 100.0

    # Prize pool share for top prize
    # V75: ~26% for 7-ratt, V85: ~23% for 8-ratt, V86: ~23% for 8-ratt
    prize_share = {"V75": 0.26, "V85": 0.23, "V86": 0.23}
    prize_pool = turnover_kr * prize_share.get(gt, 0.25)

    # Total rows in pool
    row_price = ROW_PRICES[gt]
    total_rows = turnover_kr / row_price

    # Product of winner streck fractions
    streck_product = 1.0
    for race in races:
        winner = race["winner"]
        bd = race.get("bet_dist", {})
        w_streck = bd.get(winner, 5.0)  # default 5% if missing
        streck_product *= (w_streck / 100.0)

    # Expected winning rows
    expected_winners = max(0.01, total_rows * streck_product)

    payout = prize_pool / expected_winners
    # Cap at prize pool
    payout = min(payout, prize_pool * 0.9)

    return payout


# ═══════════════════════════════════════════════════════════════
# MAIN ANALYSIS
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 78)
    print("  REALISTIC ROI: spike_easiest_rest4 — ACTUAL PAYOUTS")
    print("=" * 78)
    print()

    # Load data
    rounds = load_backtest_results()
    print(f"Loaded {len(rounds)} backtest rounds")
    for gt in ["V75", "V85", "V86"]:
        n = sum(1 for r in rounds if r["game_type"] == gt)
        print(f"  {gt}: {n} rounds")

    stats = enrich_with_cache(rounds)
    print(f"\nCache matching: {stats['matched']} matched, "
          f"{stats['no_cache']} missing, "
          f"{stats['no_payout']} jackpot/no-payout rounds")
    print()

    # Variants to compare
    variants = [
        ("Pure model (1.0)", 1.0),
        ("Hybrid 20/80 (0.20)", 0.20),
        ("Pure market (0.0)", 0.0),
    ]

    # ── Per-variant results ──
    print("=" * 78)
    print("  SYSTEM COSTS")
    print("=" * 78)
    for gt in ["V75", "V85", "V86"]:
        row_price = ROW_PRICES[gt]
        n_races = 7 if gt == "V75" else 8
        n_rows = 1 * (4 ** (n_races - 1))
        cost = row_price * n_rows
        print(f"  {gt}: {n_races} races, 1 spik + {n_races-1}x4, "
              f"{n_rows} rows x {row_price} kr = {cost:.0f} kr/round")
    print()

    # ── Detailed results per variant ──
    all_variant_results = {}

    for label, mw in variants:
        print("=" * 78)
        print(f"  {label}")
        print("=" * 78)

        total_cost = 0.0
        total_payout = 0.0
        rounds_played = 0
        rounds_won = 0
        spik_total = 0
        spik_correct = 0

        per_game = defaultdict(lambda: {
            "cost": 0.0, "payout": 0.0, "n": 0, "wins": 0,
            "spik_ok": 0, "spik_n": 0,
            "win_details": [],
            "actual_payouts_used": 0,
            "estimated_payouts_used": 0,
        })

        for rd in rounds:
            result = build_spike_easiest_rest4(rd, mw)
            if result is None:
                continue

            rounds_played += 1
            total_cost += result["cost"]
            spik_total += 1

            gt = rd["game_type"]
            pg = per_game[gt]
            pg["cost"] += result["cost"]
            pg["n"] += 1
            pg["spik_n"] += 1

            if result["spike_correct"]:
                spik_correct += 1
                pg["spik_ok"] += 1

            if result["won"]:
                rounds_won += 1
                total_payout += result["payout"]
                pg["wins"] += 1
                pg["payout"] += result["payout"]
                pg["win_details"].append({
                    "date": rd["date"],
                    "payout": result["payout"],
                    "cost": result["cost"],
                    "actual": result["actual_payout_used"],
                    "jackpot_round": result["jackpot_round"],
                })
                if result["actual_payout_used"]:
                    pg["actual_payouts_used"] += 1
                else:
                    pg["estimated_payouts_used"] += 1

        roi = (total_payout - total_cost) / total_cost * 100 if total_cost > 0 else 0
        win_rate = rounds_won / rounds_played * 100 if rounds_played > 0 else 0
        spik_pct = spik_correct / spik_total * 100 if spik_total > 0 else 0

        print(f"\n  TOTALT:")
        print(f"    Rounds: {rounds_played}, Wins: {rounds_won} ({win_rate:.1f}%)")
        print(f"    Spike hit rate: {spik_correct}/{spik_total} ({spik_pct:.1f}%)")
        print(f"    Total cost:   {total_cost:>12,.0f} kr")
        print(f"    Total payout: {total_payout:>12,.0f} kr")
        print(f"    Net P&L:      {total_payout - total_cost:>+12,.0f} kr")
        print(f"    ROI:          {roi:>+12.1f}%")

        for gt in sorted(per_game.keys()):
            g = per_game[gt]
            if g["n"] == 0:
                continue
            g_roi = (g["payout"] - g["cost"]) / g["cost"] * 100 if g["cost"] > 0 else 0
            g_wr = g["wins"] / g["n"] * 100
            g_spik = g["spik_ok"] / g["spik_n"] * 100 if g["spik_n"] > 0 else 0

            print(f"\n    {gt}:")
            print(f"      Rounds: {g['n']}, Wins: {g['wins']} ({g_wr:.1f}%)")
            print(f"      Spike: {g['spik_ok']}/{g['spik_n']} ({g_spik:.1f}%)")
            print(f"      Cost:   {g['cost']:>12,.0f} kr")
            print(f"      Payout: {g['payout']:>12,.0f} kr")
            print(f"      ROI:    {g_roi:>+12.1f}%")
            print(f"      Payouts from actual data: {g['actual_payouts_used']}, "
                  f"estimated: {g['estimated_payouts_used']}")

            if g["win_details"]:
                print(f"      Winning rounds:")
                for wd in g["win_details"]:
                    src = "ACTUAL" if wd["actual"] else "EST"
                    jp = " (jackpot round)" if wd["jackpot_round"] else ""
                    print(f"        {wd['date']}: payout {wd['payout']:>12,.0f} kr "
                          f"(cost {wd['cost']:.0f}) [{src}]{jp}")

        all_variant_results[label] = {
            "total_cost": total_cost,
            "total_payout": total_payout,
            "roi": roi,
            "rounds": rounds_played,
            "wins": rounds_won,
            "win_rate": win_rate,
            "spik_pct": spik_pct,
            "per_game": {gt: dict(per_game[gt]) for gt in per_game},
        }
        print()

    # ── Comparison table ──
    print()
    print("=" * 78)
    print("  COMPARISON SUMMARY")
    print("=" * 78)

    header = f"  {'Variant':<25} | {'Rounds':>6} | {'Wins':>5} | {'Win%':>6} | {'Spik%':>6} | {'Cost':>10} | {'Payout':>12} | {'ROI':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for label, mw in variants:
        v = all_variant_results[label]
        print(f"  {label:<25} | {v['rounds']:>6} | {v['wins']:>5} | "
              f"{v['win_rate']:>5.1f}% | {v['spik_pct']:>5.1f}% | "
              f"{v['total_cost']:>10,.0f} | {v['total_payout']:>12,.0f} | "
              f"{v['roi']:>+7.1f}%")

    # ── Per game type comparison ──
    print()
    for gt in ["V75", "V85", "V86"]:
        gt_header = f"  {gt}: {'Variant':<25} | {'Rnd':>4} | {'Win':>4} | {'Win%':>6} | {'Spik%':>6} | {'Cost':>9} | {'Payout':>12} | {'ROI':>8}"
        print(gt_header)
        print("  " + "-" * (len(gt_header) - 2))
        for label, mw in variants:
            v = all_variant_results[label]
            g = v["per_game"].get(gt, {})
            if not g or g.get("n", 0) == 0:
                continue
            g_roi = (g["payout"] - g["cost"]) / g["cost"] * 100 if g["cost"] > 0 else 0
            g_wr = g["wins"] / g["n"] * 100
            g_spik = g["spik_ok"] / g["spik_n"] * 100 if g["spik_n"] > 0 else 0
            print(f"  {gt}: {label:<25} | {g['n']:>4} | {g['wins']:>4} | "
                  f"{g_wr:>5.1f}% | {g_spik:>5.1f}% | "
                  f"{g['cost']:>9,.0f} | {g['payout']:>12,.0f} | "
                  f"{g_roi:>+7.1f}%")
        print()

    # ── Average payout per win ──
    print("=" * 78)
    print("  AVERAGE PAYOUT PER WIN")
    print("=" * 78)
    for label, mw in variants:
        v = all_variant_results[label]
        if v["wins"] > 0:
            avg = v["total_payout"] / v["wins"]
            print(f"  {label:<25}: {avg:>12,.0f} kr avg per win")
        else:
            print(f"  {label:<25}: no wins")

    # ── Break-even analysis ──
    print()
    print("=" * 78)
    print("  BREAK-EVEN ANALYSIS")
    print("=" * 78)
    for gt in ["V75", "V85", "V86"]:
        row_price = ROW_PRICES[gt]
        n_races = 7 if gt == "V75" else 8
        n_rows = 4 ** (n_races - 1)
        cost_per_round = row_price * n_rows
        print(f"\n  {gt}: Cost per round = {cost_per_round:,.0f} kr")
        for label, mw in variants:
            v = all_variant_results[label]
            g = v["per_game"].get(gt, {})
            if not g or g.get("n", 0) == 0:
                continue
            g_wr = g["wins"] / g["n"]
            if g["wins"] > 0:
                avg_payout = g["payout"] / g["wins"]
                # Break-even: cost_per_round / avg_payout = required win rate
                be_wr = cost_per_round / avg_payout if avg_payout > 0 else 999
                print(f"    {label:<25}: win rate {g_wr:.1%}, "
                      f"avg payout {avg_payout:>10,.0f} kr, "
                      f"break-even needs {be_wr:.1%} win rate")
            else:
                print(f"    {label:<25}: no wins (win rate {g_wr:.1%})")


if __name__ == "__main__":
    main()
