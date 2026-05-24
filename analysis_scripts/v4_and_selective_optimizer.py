#!/usr/bin/env python3
"""V4 + Selektiv V75 — hitta spelformer där modellen KAN vara lönsam.

V75/V85 system kräver 55,000kr/rad i snitt — omöjligt.
Men:
  V4 = 4 avdelningar → top3^4 = 10.7% hit rate → breakeven ~47kr/rad
  V64 = 6 avdelningar → top3^6 = 3.5% hit rate → breakeven ~570kr/rad
  V5 = 5 avdelningar → top3^5 = 6.1% hit rate → breakeven ~164kr/rad

Plus: selektiv V75 (bara spela "säkra" omgångar).

Dessutom: enskilda lopp — vinstspel/plats med modell-edge.
"""

import asyncio
import json
import glob
import sys
import re
from datetime import date, datetime
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import DEFAULT_CONFIG

CACHE_DIR = Path("cache")


def find_rounds_by_type(game_types: list[str], start_year=2024):
    """Find game rounds from calendar cache."""
    cal_pattern = re.compile(r'^(\d{4})-\d{2}-\d{2}_[a-f0-9]+\.json$')
    rounds = []

    for fp in sorted(glob.glob(str(CACHE_DIR / "*.json"))):
        fname = Path(fp).name
        m = cal_pattern.match(fname)
        if not m:
            continue
        year = int(m.group(1))
        if year < start_year:
            continue

        try:
            with open(fp) as f:
                data = json.load(f)
        except Exception:
            continue

        games = data.get("games", {})
        if not isinstance(games, dict):
            continue

        for gt in game_types:
            if gt not in games:
                continue
            game_list = games[gt]
            if not isinstance(game_list, list):
                continue
            for g in game_list:
                if g.get("status") == "results" and g.get("races"):
                    rounds.append({
                        "game_type": gt,
                        "game_id": g["id"],
                        "date": data.get("date", ""),
                    })
    return rounds


async def backtest_rounds(rounds_info: list[dict]):
    """Run v6 model backtest on rounds."""
    client = ATGClient()
    analyzer = CompositeAnalyzer(DEFAULT_CONFIG)
    results_by_type = defaultdict(list)
    processed = 0

    for rnd in rounds_info:
        gt = rnd["game_type"]
        game_id = rnd["game_id"]

        try:
            parts = game_id.split("_")
            day = date.fromisoformat(parts[1])
            game_round = await client.fetch_full_round(parts[0], day)
        except Exception:
            continue

        if not game_round or not game_round.is_finished:
            continue

        processed += 1

        # Anti-leakage
        for race in game_round.races:
            for entry in race.entries:
                original = entry.horse.past_starts
                filtered = [s for s in original if s.start_date < race.race_date]
                if len(filtered) < len(original):
                    entry.horse.past_starts = filtered
                entry.horse.recompute_career_from_starts()
            for entry in race.entries:
                entry.bet_percentage = None
                entry.odds = None

        analyzer.analyze_round(game_round)

        round_data = []
        for race in game_round.races:
            sorted_e = sorted(race.active_entries, key=lambda e: e.super_score, reverse=True)
            winner = race.result_order[0] if race.result_order else None
            if winner is None:
                continue

            ranking = [e.post_position for e in sorted_e]
            wr = ranking.index(winner) + 1 if winner in ranking else len(ranking)
            gap = sorted_e[0].super_score - sorted_e[1].super_score if len(sorted_e) >= 2 else 0
            scores = {e.post_position: e.super_score for e in sorted_e}

            round_data.append({
                "race_number": race.race_number,
                "num_starters": len(sorted_e),
                "winner": winner,
                "winner_rank": wr,
                "gap_1_2": gap,
                "top1_hit": ranking[0] == winner,
                "top2_hit": winner in ranking[:2],
                "top3_hit": winner in ranking[:3],
                "top4_hit": winner in ranking[:4],
                "top5_hit": winner in ranking[:5],
                "scores": scores,
                "top_score": sorted_e[0].super_score,
                "avg_score": sum(e.super_score for e in sorted_e) / len(sorted_e),
            })

        if round_data:
            results_by_type[gt].append({
                "date": rnd["date"],
                "game_id": game_id,
                "races": round_data,
            })

        if processed % 20 == 0:
            counts = {k: len(v) for k, v in results_by_type.items()}
            print(f"  {processed} processed: {counts}")

    print(f"  Done: {processed} rounds")
    return dict(results_by_type)


def analyze_game_type(results: list[dict], game_type: str, num_legs: int, row_price: float):
    """Full analysis for a game type."""
    all_races = []
    for r in results:
        all_races.extend(r["races"])

    if not all_races:
        return

    n = len(all_races)
    top1 = sum(1 for r in all_races if r["top1_hit"]) / n
    top2 = sum(1 for r in all_races if r["top2_hit"]) / n
    top3 = sum(1 for r in all_races if r["top3_hit"]) / n
    top4 = sum(1 for r in all_races if r["top4_hit"]) / n

    print(f"\n{'='*70}")
    print(f"  {game_type} — {len(results)} omgångar, {n} lopp")
    print(f"  {num_legs} avdelningar, {row_price:.2f} kr/rad")
    print(f"{'='*70}")
    print(f"  Top-1: {top1:.1%}  Top-2: {top2:.1%}  Top-3: {top3:.1%}  Top-4: {top4:.1%}")

    # System simulation with gap-based assignment
    print(f"\n  --- SYSTEM SIMULERING ---")
    print(f"  {'Strategi':<25} {'Rader':>6} {'Kostnad':>8} {'Träff':>7} {'Hits':>4} {'Breakeven':>10} {'OK?':>4}")
    print(f"  {'-'*68}")

    best_strategies = []

    for n_spik in range(0, min(num_legs + 1, 5)):
        for bw in range(2, 8):
            n_bred = num_legs - n_spik
            total_rows = bw ** n_bred if n_bred > 0 else 1
            cost = total_rows * row_price
            if cost < 1 or cost > 5000:
                continue

            hits = 0
            total = 0
            for rnd in results:
                races = rnd["races"]
                if len(races) < num_legs:
                    continue
                total += 1
                indexed = sorted(enumerate(races[:num_legs]),
                                 key=lambda x: x[1]["gap_1_2"], reverse=True)
                ok = True
                for rank, (_, race) in enumerate(indexed):
                    need = 1 if rank < n_spik else bw
                    if race["winner_rank"] > need:
                        ok = False
                        break
                if ok:
                    hits += 1

            if total == 0 or hits == 0:
                continue

            hit_rate = hits / total
            breakeven = cost / hit_rate

            best_strategies.append({
                "name": f"{n_spik}S+{n_bred}×{bw}" if n_bred > 0 else f"{n_spik}S",
                "rows": total_rows,
                "cost": cost,
                "hit_rate": hit_rate,
                "hits": hits,
                "total": total,
                "breakeven": breakeven,
            })

    best_strategies.sort(key=lambda s: s["breakeven"])

    for s in best_strategies:
        ok = "✅" if s["breakeven"] < 300 else "⚠️" if s["breakeven"] < 2000 else "❌"
        print(f"  {s['name']:<25} {s['rows']:>6} {s['cost']:>7.0f}kr {s['hit_rate']:>6.1%} {s['hits']:>4} {s['breakeven']:>9.0f}kr {ok:>3}")

    # ROI table
    print(f"\n  --- ROI (utdelning per rad) ---")

    # Typical payouts by game type
    typical_payouts = {
        "V4": {"low": 50, "med": 200, "high": 500, "avg": 150},
        "V5": {"low": 100, "med": 300, "high": 1000, "avg": 250},
        "V64": {"low": 50, "med": 200, "high": 800, "avg": 180},
        "V65": {"low": 100, "med": 400, "high": 2000, "avg": 350},
    }

    payouts = [50, 100, 200, 500, 1000, 2000]
    print(f"  {'Strategi':<22} {'Kost':>6}", end="")
    for p in payouts:
        print(f" {p:>6}kr", end="")
    print()
    print(f"  {'-'*80}")

    for s in best_strategies[:10]:
        print(f"  {s['name']:<22} {s['cost']:>5.0f}kr", end="")
        for payout in payouts:
            roi = (s["hit_rate"] * payout - s["cost"]) / s["cost"]
            if roi >= 0:
                print(f"  🟢{roi:+.0%}", end="")
            elif roi > -0.5:
                print(f"  🟡{roi:+.0%}", end="")
            else:
                print(f"  🔴{roi:+.0%}", end="")
        print()

    return best_strategies


def selective_v75_analysis(v75_results: list[dict]):
    """Selektiv V75 — bara spela 'säkra' omgångar."""
    if not v75_results:
        return

    print(f"\n\n{'='*70}")
    print(f"  SELEKTIV V75 — Bara spela 'säkra' omgångar")
    print(f"  Hypotes: filtrera omgångar med högt modellförtroende")
    print(f"{'='*70}")

    # For each round, compute metrics
    round_metrics = []
    for rnd in v75_results:
        races = rnd["races"]
        if len(races) < 7:
            continue

        avg_gap = sum(r["gap_1_2"] for r in races[:7]) / 7
        min_gap = min(r["gap_1_2"] for r in races[:7])
        avg_top_score = sum(r["top_score"] for r in races[:7]) / 7
        top1_count = sum(1 for r in races[:7] if r["top1_hit"])
        top3_count = sum(1 for r in races[:7] if r["top3_hit"])

        round_metrics.append({
            "date": rnd["date"],
            "avg_gap": avg_gap,
            "min_gap": min_gap,
            "avg_top_score": avg_top_score,
            "top1_count": top1_count,
            "top3_count": top3_count,
            "races": races[:7],
        })

    # Test different selection criteria
    print(f"\n  Total omgångar: {len(round_metrics)}")

    criteria = [
        ("Alla omgångar", lambda m: True),
        ("avg_gap >= 8", lambda m: m["avg_gap"] >= 8),
        ("avg_gap >= 10", lambda m: m["avg_gap"] >= 10),
        ("avg_gap >= 12", lambda m: m["avg_gap"] >= 12),
        ("avg_gap >= 15", lambda m: m["avg_gap"] >= 15),
        ("min_gap >= 3", lambda m: m["min_gap"] >= 3),
        ("min_gap >= 5", lambda m: m["min_gap"] >= 5),
        ("min_gap >= 7", lambda m: m["min_gap"] >= 7),
        ("avg_top_score >= 70", lambda m: m["avg_top_score"] >= 70),
        ("avg_top_score >= 75", lambda m: m["avg_top_score"] >= 75),
        ("avg_top_score >= 80", lambda m: m["avg_top_score"] >= 80),
    ]

    print(f"\n  {'Kriterium':<25} {'Omg':>4} {'Avg top3':>8} {'2S+5×3 träff':>12} {'BE (2S+5×3)':>12}")
    print(f"  {'-'*65}")

    for name, criterion in criteria:
        selected = [m for m in round_metrics if criterion(m)]
        if not selected:
            continue

        # Per-race top3 rate
        all_top3 = sum(r["top3_count"] for r in selected) / (len(selected) * 7)

        # System simulation: 2S+5×3 (2 spikar, 5 avd × 3 hästar, cost=121.50kr)
        hits_2s5x3 = 0
        for m in selected:
            indexed = sorted(enumerate(m["races"][:7]),
                             key=lambda x: x[1]["gap_1_2"], reverse=True)
            ok = True
            for rank, (_, race) in enumerate(indexed):
                need = 1 if rank < 2 else 3
                if race["winner_rank"] > need:
                    ok = False
                    break
            if ok:
                hits_2s5x3 += 1

        n_sel = len(selected)
        hit_rate = hits_2s5x3 / n_sel if n_sel > 0 else 0
        cost = 3**5 * 0.50  # 243 × 0.50 = 121.50kr
        breakeven = cost / hit_rate if hit_rate > 0 else float('inf')

        be_str = f"{breakeven:.0f}kr/rad" if breakeven < 1e6 else "∞"
        print(f"  {name:<25} {n_sel:>4} {all_top3:>7.1%} {hit_rate:>11.1%} {be_str:>12}")

    # Best selective strategy
    print(f"\n  --- BÄSTA SELEKTIVA FILTER ---")

    for gap_thresh in [8, 10, 12, 15]:
        selected = [m for m in round_metrics if m["avg_gap"] >= gap_thresh]
        if len(selected) < 5:
            continue

        print(f"\n  Filter: avg_gap >= {gap_thresh} ({len(selected)}/{len(round_metrics)} omgångar)")

        for n_spik in [1, 2, 3]:
            for bw in [2, 3, 4]:
                total_rows = bw ** (7 - n_spik)
                cost = total_rows * 0.50

                hits = 0
                for m in selected:
                    indexed = sorted(enumerate(m["races"][:7]),
                                     key=lambda x: x[1]["gap_1_2"], reverse=True)
                    ok = True
                    for rank, (_, race) in enumerate(indexed):
                        need = 1 if rank < n_spik else bw
                        if race["winner_rank"] > need:
                            ok = False
                            break
                    if ok:
                        hits += 1

                if hits == 0:
                    continue

                hit_rate = hits / len(selected)
                breakeven = cost / hit_rate
                freq = len(selected) / len(round_metrics) * 52  # rounds/year

                ok = "✅" if breakeven < 2000 else "⚠️" if breakeven < 10000 else "❌"
                print(f"    {n_spik}S+{7-n_spik}×{bw}: {total_rows:>5}r {cost:>6.0f}kr "
                      f"träff {hit_rate:.1%} ({hits}/{len(selected)}) "
                      f"BE {breakeven:.0f}kr/rad "
                      f"~{freq:.0f} ggr/år {ok}")


def single_race_edge(all_results: dict):
    """Enskilda lopp — vinstspel/plats med modell-edge."""
    print(f"\n\n{'='*70}")
    print(f"  ENSKILDA LOPP — Plats/Vinstspel med modell-edge")
    print(f"  Hypotes: modellens #1 vinner 27% — odds ofta 3-4x → edge?")
    print(f"{'='*70}")

    all_races = []
    for gt, results in all_results.items():
        for rnd in results:
            for race in rnd["races"]:
                race["game_type"] = gt
                all_races.append(race)

    if not all_races:
        return

    n = len(all_races)
    print(f"  Totalt {n} lopp")

    # Simulate flat bet on model's #1 pick
    print(f"\n  --- FLAT BET på modellens #1 ---")
    print(f"  Win rate: {sum(1 for r in all_races if r['top1_hit']) / n:.1%}")

    # Group by gap ranges
    print(f"\n  Win rate per gap-nivå:")
    for lo, hi, label in [(0, 5, "Gap 0-5"), (5, 10, "Gap 5-10"),
                           (10, 15, "Gap 10-15"), (15, 20, "Gap 15-20"),
                           (20, 50, "Gap 20+")]:
        subset = [r for r in all_races if lo <= r["gap_1_2"] < hi]
        if len(subset) < 20:
            continue
        wr = sum(1 for r in subset if r["top1_hit"]) / len(subset)

        # Implied odds needed for breakeven on vinstspel
        # Take-out on vinstspel ~25%, so if true prob = wr:
        # Breakeven odds = 1/wr. Fair odds = 1/wr. Offered odds = 1/(wr * 0.75) approx.
        # Profit if offered odds > 1/wr → positive edge
        implied_fair_odds = 1 / wr if wr > 0 else 999
        print(f"    {label:<12} {len(subset):>5} lopp  win {wr:.1%}  fair odds {implied_fair_odds:.1f}x")

    # Group by top score
    print(f"\n  Win rate per modellpoäng:")
    for lo, hi, label in [(0, 60, "Score <60"), (60, 70, "Score 60-70"),
                           (70, 80, "Score 70-80"), (80, 90, "Score 80-90"),
                           (90, 100, "Score 90+")]:
        subset = [r for r in all_races if lo <= r["top_score"] < hi]
        if len(subset) < 10:
            continue
        wr = sum(1 for r in subset if r["top1_hit"]) / len(subset)
        implied_fair_odds = 1 / wr if wr > 0 else 999
        print(f"    {label:<12} {len(subset):>5} lopp  win {wr:.1%}  fair odds {implied_fair_odds:.1f}x")

    # Combined filters
    print(f"\n  Win rate med kombinerade filter:")
    filters = [
        ("Gap >= 15", lambda r: r["gap_1_2"] >= 15),
        ("Gap >= 15 + Score >= 80", lambda r: r["gap_1_2"] >= 15 and r["top_score"] >= 80),
        ("Gap >= 10 + Score >= 75", lambda r: r["gap_1_2"] >= 10 and r["top_score"] >= 75),
        ("Gap >= 10 + Score >= 80", lambda r: r["gap_1_2"] >= 10 and r["top_score"] >= 80),
        ("Gap >= 20", lambda r: r["gap_1_2"] >= 20),
        ("Gap >= 12 + fält ≤ 10", lambda r: r["gap_1_2"] >= 12 and r["num_starters"] <= 10),
        ("Gap >= 15 + fält ≤ 10", lambda r: r["gap_1_2"] >= 15 and r["num_starters"] <= 10),
    ]

    for label, filt in filters:
        subset = [r for r in all_races if filt(r)]
        if len(subset) < 10:
            continue
        wr = sum(1 for r in subset if r["top1_hit"]) / len(subset)
        freq = len(subset) / n
        implied = 1 / wr if wr > 0 else 999

        # Simulera: 100kr flat bet per lopp
        # Antag odds = 3.0 i snitt (favorit-nivå)
        # Vinst om wr > 1/odds → wr > 33% behövs vid odds 3.0
        # Vid odds 2.5: wr > 40% behövs
        # Vid odds 4.0: wr > 25% behövs
        profit_at_3 = (wr * 3.0 - 1) * 100  # per 100kr bet
        profit_at_4 = (wr * 4.0 - 1) * 100
        profit_at_2_5 = (wr * 2.5 - 1) * 100

        indicator = "🟢" if wr >= 0.35 else "🟡" if wr >= 0.28 else "🔴"
        print(f"    {indicator} {label:<30} {len(subset):>4} lopp ({freq:.0%})  win {wr:.1%}  "
              f"odds 3.0:{profit_at_3:+.0f}kr  4.0:{profit_at_4:+.0f}kr")


async def main():
    print("=" * 80)
    print("  ALTERNATIVA SPELFORMER — V4, V64, Selektiv V75, Enskilda lopp")
    print("=" * 80)

    # Find rounds — SAMPLE V4/V5 (too many to process all)
    round_types = ["V75", "V85", "V4", "V64", "V65", "V5"]
    all_rounds = find_rounds_by_type(round_types, start_year=2024)

    # Sample: max 80 rounds per game type for fast processing
    import random
    random.seed(42)
    by_type = defaultdict(list)
    for r in all_rounds:
        by_type[r["game_type"]].append(r)

    MAX_PER_TYPE = 80
    rounds_info = []
    for gt, rlist in by_type.items():
        if len(rlist) > MAX_PER_TYPE:
            sampled = random.sample(rlist, MAX_PER_TYPE)
            print(f"  {gt}: sampled {MAX_PER_TYPE}/{len(rlist)}")
            rounds_info.extend(sampled)
        else:
            print(f"  {gt}: all {len(rlist)}")
            rounds_info.extend(rlist)

    counts = defaultdict(int)
    for r in rounds_info:
        counts[r["game_type"]] += 1
    print(f"\nHittade omgångar: {dict(counts)}")

    # Run backtests
    all_results = await backtest_rounds(rounds_info)

    # Analyze each game type
    game_configs = {
        "V4": (4, 0.50),
        "V5": (5, 0.50),
        "V64": (6, 1.00),
        "V65": (6, 0.50),
        "V75": (7, 0.50),
        "V85": (8, 0.50),
    }

    for gt in ["V4", "V5", "V64", "V65"]:
        if gt not in all_results:
            continue
        legs, price = game_configs[gt]
        analyze_game_type(all_results[gt], gt, legs, price)

    # Selective V75
    v75_data = all_results.get("V75", []) + all_results.get("V85", [])
    if v75_data:
        selective_v75_analysis(v75_data)

    # Single race edge
    if all_results:
        single_race_edge(all_results)

    # FINAL VERDICT
    print(f"\n\n{'='*80}")
    print(f"  ╔══════════════════════════════════════════════════════════════╗")
    print(f"  ║   SLUTVÄRDERING: VAR KAN MODELLEN TJÄNA PENGAR?           ║")
    print(f"  ╚══════════════════════════════════════════════════════════════╝")
    print(f"{'='*80}")

    for gt in ["V4", "V5", "V64", "V65", "V75", "V85"]:
        if gt not in all_results:
            continue
        results = all_results[gt]
        all_races = []
        for r in results:
            all_races.extend(r["races"])
        n = len(all_races)
        if n == 0:
            continue
        top1 = sum(1 for r in all_races if r["top1_hit"]) / n
        top3 = sum(1 for r in all_races if r["top3_hit"]) / n
        legs, price = game_configs[gt]
        sys_hit = top3 ** legs
        cost_3 = 3**legs * price
        breakeven = cost_3 / sys_hit if sys_hit > 0 else float('inf')
        be_str = f"{breakeven:.0f}" if breakeven < 1e6 else "∞"

        verdict = "🟢 MÖJLIGT" if breakeven < 500 else "🟡 SVÅRT" if breakeven < 5000 else "🔴 OMÖJLIGT"

        print(f"  {gt:<6} {n:>4} lopp  top1={top1:.0%} top3={top3:.0%}  "
              f"system(3^{legs}): {sys_hit:.1%} hit  "
              f"BE: {be_str}kr/rad  {verdict}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
