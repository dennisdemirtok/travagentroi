#!/usr/bin/env python3
"""
Large-scale scoring weight backtest.

Tests different model/market weight ratios using ALL available backtest data
(V85, V86, V75 — 205 rounds, 1559 races).

For each race:
  super_score(w) = w * composite_score_normalized + (1-w) * market_score_normalized

  composite_score_normalized = composite / 100
  market_score_normalized = streck / max_streck_in_race

Measures:
  - Rank-1 win%: how often our #1 pick wins
  - Top-3%: how often the actual winner is in our top 3
  - Top-5%: how often the actual winner is in our top 5
"""

import json
import sys
from pathlib import Path

BACKTEST_DIR = Path(__file__).parent.parent / "backtest_results"

FILES = [
    BACKTEST_DIR / "backtest_V85_2024-01-01_2026-04-01.json",
    BACKTEST_DIR / "backtest_V86_2024-01-01_2026-04-01.json",
    BACKTEST_DIR / "backtest_V75_2024-01-01_2026-04-01.json",
]

WEIGHTS = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60, 0.75, 0.90, 1.0]


def load_all_races():
    """Load all races from backtest JSON files."""
    races = []
    for f in FILES:
        if not f.exists():
            print(f"  SKIP: {f.name} not found", file=sys.stderr)
            continue
        with open(f) as fh:
            data = json.load(fh)
        game_type = data["game_type"]
        for rnd in data["predictions"]:
            date = rnd["date"]
            for race in rnd["races"]:
                actual = race.get("actual_result", [])
                scores = race.get("horse_scores", {})
                bets = race.get("horse_bet_pct", {})
                if not actual or not scores:
                    continue
                winner = actual[0]
                # Only include races where winner has scores
                if str(winner) not in scores and winner not in scores:
                    continue
                races.append({
                    "game_type": game_type,
                    "date": date,
                    "race_number": race.get("race_number", "?"),
                    "winner": winner,
                    "scores": {int(k): float(v) for k, v in scores.items()},
                    "bets": {int(k): float(v) for k, v in bets.items()},
                })
    return races


def rank_by_weight(race, model_weight):
    """Rank horses using blended super_score at given model weight."""
    scores = race["scores"]
    bets = race["bets"]

    # Normalize composite scores to 0-1
    max_comp = max(scores.values()) if scores else 1
    min_comp = min(scores.values()) if scores else 0
    comp_range = max_comp - min_comp if max_comp > min_comp else 1

    # Normalize market (streckprocent) to 0-1
    max_bet = max(bets.values()) if bets else 1

    ranked = []
    for horse_num in scores:
        comp_norm = (scores[horse_num] - min_comp) / comp_range
        bet_norm = bets.get(horse_num, 0) / max_bet if max_bet > 0 else 0

        blended = model_weight * comp_norm + (1 - model_weight) * bet_norm
        ranked.append((horse_num, blended))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return [h[0] for h in ranked]


def rank_by_market_only(race):
    """Rank purely by streckprocent."""
    bets = race["bets"]
    ranked = sorted(bets.items(), key=lambda x: x[1], reverse=True)
    return [h[0] for h in ranked]


def evaluate(races, weight):
    """Evaluate a weight across all races."""
    rank1_wins = 0
    top3_wins = 0
    top5_wins = 0
    total = 0

    for race in races:
        winner = race["winner"]
        if weight == "market":
            ranking = rank_by_market_only(race)
        else:
            ranking = rank_by_weight(race, weight)

        if not ranking:
            continue
        total += 1

        if ranking[0] == winner:
            rank1_wins += 1
        if winner in ranking[:3]:
            top3_wins += 1
        if winner in ranking[:5]:
            top5_wins += 1

    return {
        "rank1_pct": rank1_wins / total * 100 if total else 0,
        "top3_pct": top3_wins / total * 100 if total else 0,
        "top5_pct": top5_wins / total * 100 if total else 0,
        "total": total,
    }


def evaluate_by_game_type(races, weight):
    """Evaluate per game type."""
    by_type = {}
    for race in races:
        gt = race["game_type"]
        if gt not in by_type:
            by_type[gt] = []
        by_type[gt].append(race)

    results = {}
    for gt, gt_races in sorted(by_type.items()):
        results[gt] = evaluate(gt_races, weight)
    return results


def main():
    print("Laddar backtestdata...")
    races = load_all_races()
    print(f"Totalt: {len(races)} lopp\n")

    # Count by type
    by_type = {}
    for r in races:
        gt = r["game_type"]
        by_type[gt] = by_type.get(gt, 0) + 1
    for gt, count in sorted(by_type.items()):
        print(f"  {gt}: {count} lopp")
    print()

    # ============================================================
    # OVERALL RESULTS
    # ============================================================
    print("=" * 80)
    print(f"RESULTAT: {len(races)} lopp totalt")
    print("=" * 80)
    print(f"{'Viktning':<30} {'Rank1%':>8} {'Top3%':>8} {'Top5%':>8} {'Lopp':>6}")
    print(f"{'-'*30} {'-'*8} {'-'*8} {'-'*8} {'-'*6}")

    # Pure market
    res = evaluate(races, "market")
    print(f"{'Ren marknad (streck)':<30} {res['rank1_pct']:>7.1f}% {res['top3_pct']:>7.1f}% {res['top5_pct']:>7.1f}% {res['total']:>6}")

    results = {}
    best_rank1 = ("", 0)
    best_top3 = ("", 0)
    best_top5 = ("", 0)

    for w in WEIGHTS:
        label = f"Modell {int(w*100)}% + Mark {int((1-w)*100)}%"
        if w == 0.25:
            label += " (nu)"
        res = evaluate(races, w)
        results[w] = res
        print(f"{label:<30} {res['rank1_pct']:>7.1f}% {res['top3_pct']:>7.1f}% {res['top5_pct']:>7.1f}% {res['total']:>6}")

        if res["rank1_pct"] > best_rank1[1]:
            best_rank1 = (label, res["rank1_pct"])
        if res["top3_pct"] > best_top3[1]:
            best_top3 = (label, res["top3_pct"])
        if res["top5_pct"] > best_top5[1]:
            best_top5 = (label, res["top5_pct"])

    print()
    print(f"Bäst Rank-1: {best_rank1[0]} ({best_rank1[1]:.1f}%)")
    print(f"Bäst Top-3:  {best_top3[0]} ({best_top3[1]:.1f}%)")
    print(f"Bäst Top-5:  {best_top5[0]} ({best_top5[1]:.1f}%)")

    # ============================================================
    # PER GAME TYPE
    # ============================================================
    print()
    print("=" * 80)
    print("PER SPELTYP")
    print("=" * 80)

    for gt in sorted(by_type.keys()):
        gt_races = [r for r in races if r["game_type"] == gt]
        print(f"\n--- {gt} ({len(gt_races)} lopp) ---")
        print(f"{'Viktning':<30} {'Rank1%':>8} {'Top3%':>8} {'Top5%':>8}")
        print(f"{'-'*30} {'-'*8} {'-'*8} {'-'*8}")

        for w in [0.0, 0.10, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.75, 1.0]:
            label = f"Modell {int(w*100)}% + Mark {int((1-w)*100)}%"
            if w == 0.25:
                label += " (nu)"
            res = evaluate(gt_races, w)
            print(f"{label:<30} {res['rank1_pct']:>7.1f}% {res['top3_pct']:>7.1f}% {res['top5_pct']:>7.1f}%")

    # ============================================================
    # SCORE DELTA ANALYSIS: Model advantage over pure market
    # ============================================================
    print()
    print("=" * 80)
    print("MODELL-EDGE vs REN MARKNAD (delta procentenheter)")
    print("=" * 80)
    baseline = evaluate(races, "market")
    print(f"{'Viktning':<30} {'ΔRank1':>8} {'ΔTop3':>8} {'ΔTop5':>8}")
    print(f"{'-'*30} {'-'*8} {'-'*8} {'-'*8}")

    for w in WEIGHTS:
        if w == 0.0:
            continue
        label = f"Modell {int(w*100)}%"
        if w == 0.25:
            label += " (nu)"
        res = results[w]
        d1 = res["rank1_pct"] - baseline["rank1_pct"]
        d3 = res["top3_pct"] - baseline["top3_pct"]
        d5 = res["top5_pct"] - baseline["top5_pct"]
        sign1 = "+" if d1 >= 0 else ""
        sign3 = "+" if d3 >= 0 else ""
        sign5 = "+" if d5 >= 0 else ""
        print(f"{label:<30} {sign1}{d1:>6.1f}pp {sign3}{d3:>6.1f}pp {sign5}{d5:>6.1f}pp")


if __name__ == "__main__":
    main()
