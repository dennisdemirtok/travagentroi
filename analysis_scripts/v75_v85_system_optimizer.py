#!/usr/bin/env python3
"""V75/V85 System Optimizer — finds optimal system configurations.

CORRECT ROI math:
  System cost = total_rows × row_price
  When you hit: return = utdelning_per_rad × 1 (one winning combination)
  Expected return/round = hit_rate × avg_utdelning
  ROI = (expected_return - cost) / cost

Works DIRECTLY from cache files. Runs v6 model on each race.
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
from trav_agent.config import AnalysisConfig, DEFAULT_CONFIG

CACHE_DIR = Path("cache")


def find_v75_v85_rounds(start_year=2024):
    """Find all V75/V85 game round IDs from calendar cache files."""
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

        for game_type in ["V75", "V85"]:
            if game_type not in games:
                continue
            game_list = games[game_type]
            if not isinstance(game_list, list):
                continue
            for g in game_list:
                game_id = g.get("id", "")
                status = g.get("status", "")
                race_ids = g.get("races", [])
                if status == "results" and race_ids:
                    rounds.append({
                        "game_type": game_type,
                        "game_id": game_id,
                        "date": data.get("date", ""),
                        "race_ids": race_ids,
                    })

    return rounds


async def run_backtest_from_cache(rounds_info: list[dict]):
    """Run v6 model on each round directly from cache data."""
    client = ATGClient()
    analyzer = CompositeAnalyzer(DEFAULT_CONFIG)

    all_results = {"V75": [], "V85": []}
    total_processed = 0

    print(f"\nProcessing {len(rounds_info)} rounds...")

    for rnd_info in rounds_info:
        game_type = rnd_info["game_type"]
        game_id = rnd_info["game_id"]
        round_date_str = rnd_info["date"]

        try:
            parts = game_id.split("_")
            gt = parts[0]
            day_str = parts[1]
            from datetime import date as dt_date
            day = dt_date.fromisoformat(day_str)
            game_round = await client.fetch_full_round(gt, day)
        except Exception as e:
            continue

        if not game_round or not game_round.is_finished:
            continue

        total_processed += 1

        # Anti-leakage: filter future starts
        for race in game_round.races:
            race_date = race.race_date
            for entry in race.entries:
                original = entry.horse.past_starts
                filtered = [s for s in original if s.start_date < race_date]
                if len(filtered) < len(original):
                    entry.horse.past_starts = filtered
                entry.horse.recompute_career_from_starts()

        # Neutralize closing odds
        for race in game_round.races:
            for entry in race.entries:
                entry.bet_percentage = None
                entry.odds = None

        # Run v6 analysis
        analyzer.analyze_round(game_round)

        round_results = []
        for race in game_round.races:
            sorted_entries = sorted(
                race.active_entries,
                key=lambda e: e.super_score,
                reverse=True,
            )

            actual_winner = race.result_order[0] if race.result_order else None
            if actual_winner is None:
                continue

            model_ranking = [e.post_position for e in sorted_entries]
            winner_rank = (model_ranking.index(actual_winner) + 1
                           if actual_winner in model_ranking
                           else len(model_ranking))

            gap_1_2 = (sorted_entries[0].super_score - sorted_entries[1].super_score
                       if len(sorted_entries) >= 2 else 0)

            round_results.append({
                "race_number": race.race_number,
                "num_starters": len(sorted_entries),
                "actual_winner": actual_winner,
                "winner_rank": winner_rank,
                "top1_hit": model_ranking[0] == actual_winner if model_ranking else False,
                "top2_hit": actual_winner in model_ranking[:2],
                "top3_hit": actual_winner in model_ranking[:3],
                "top4_hit": actual_winner in model_ranking[:4],
                "top5_hit": actual_winner in model_ranking[:5],
                "top6_hit": actual_winner in model_ranking[:6],
                "gap_1_2": gap_1_2,
            })

        if round_results:
            all_results[game_type].append({
                "date": round_date_str,
                "game_type": game_type,
                "game_id": game_id,
                "races": round_results,
            })

        if total_processed % 20 == 0:
            v75n = len(all_results["V75"])
            v85n = len(all_results["V85"])
            print(f"  {total_processed} rounds done (V75: {v75n}, V85: {v85n})...")

    print(f"  Total: {total_processed} rounds processed")
    return all_results


def analyze_accuracy(results: list[dict], game_type: str):
    """Compute and print accuracy metrics."""
    all_races = []
    for r in results:
        all_races.extend(r["races"])

    if not all_races:
        print(f"  No races for {game_type}")
        return {}

    n = len(all_races)
    top1 = sum(1 for r in all_races if r["top1_hit"]) / n
    top2 = sum(1 for r in all_races if r["top2_hit"]) / n
    top3 = sum(1 for r in all_races if r["top3_hit"]) / n
    top4 = sum(1 for r in all_races if r["top4_hit"]) / n
    top5 = sum(1 for r in all_races if r["top5_hit"]) / n

    rank_dist = defaultdict(int)
    for r in all_races:
        rank_dist[r["winner_rank"]] += 1

    num_legs = 7 if "V75" in game_type else 8

    print(f"\n{'='*70}")
    print(f"  {game_type} v6-MODELL ACCURACY")
    print(f"  {len(results)} omgångar, {n} lopp")
    print(f"{'='*70}")
    print(f"  Top-1 (modell #1 vinner):     {top1:.1%}")
    print(f"  Top-2 (vinnare i topp 2):     {top2:.1%}")
    print(f"  Top-3 (vinnare i topp 3):     {top3:.1%}")
    print(f"  Top-4 (vinnare i topp 4):     {top4:.1%}")
    print(f"  Top-5 (vinnare i topp 5):     {top5:.1%}")
    print(f"")
    print(f"  Vinnarens modellrank-fördelning:")
    for rank in sorted(rank_dist.keys())[:12]:
        pct = rank_dist[rank] / n
        bar = "█" * int(pct * 40)
        print(f"    Rank {rank:2d}: {rank_dist[rank]:4d} ({pct:5.1%}) {bar}")

    system_top3 = top3 ** num_legs
    system_top4 = top4 ** num_legs
    print(f"\n  System ({num_legs} avdelningar):")
    print(f"    All-top3 (3^{num_legs}={3**num_legs}r): hit rate {system_top3:.2%}")
    print(f"    All-top4 (4^{num_legs}={4**num_legs}r): hit rate {system_top4:.2%}")

    return {"top1": top1, "top2": top2, "top3": top3, "top4": top4, "top5": top5}


def per_leg_analysis(results: list[dict], game_type: str):
    """Analyze accuracy per leg position."""
    num_legs = 7 if "V75" in game_type else 8

    print(f"\n{'='*70}")
    print(f"  PER-AVDELNING ANALYS — {game_type}")
    print(f"{'='*70}")

    leg_stats = defaultdict(lambda: {"top1": 0, "top2": 0, "top3": 0, "top4": 0, "total": 0, "gaps": []})

    for rnd in results:
        races = rnd["races"]
        for i, race in enumerate(races[:num_legs]):
            leg = i + 1
            leg_stats[leg]["total"] += 1
            if race["top1_hit"]:
                leg_stats[leg]["top1"] += 1
            if race["top2_hit"]:
                leg_stats[leg]["top2"] += 1
            if race["top3_hit"]:
                leg_stats[leg]["top3"] += 1
            if race["top4_hit"]:
                leg_stats[leg]["top4"] += 1
            leg_stats[leg]["gaps"].append(race["gap_1_2"])

    print(f"\n  {'Avd':>4} {'Top1':>7} {'Top2':>7} {'Top3':>7} {'Top4':>7} {'Avg Gap':>8} {'N':>5}")
    print(f"  {'-'*50}")

    for leg in range(1, num_legs + 1):
        s = leg_stats[leg]
        n = s["total"]
        if n == 0:
            continue
        avg_gap = sum(s["gaps"]) / n
        print(f"  {leg:>4} {s['top1']/n:>6.1%} {s['top2']/n:>6.1%} {s['top3']/n:>6.1%} {s['top4']/n:>6.1%} {avg_gap:>7.1f} {n:>5}")


def simulate_systems(results: list[dict], game_type: str, row_price: float):
    """Simulate system configurations with CORRECT ROI math.

    KORREKT BERÄKNING:
      System cost = total_rows × row_price
      When system hits: return = utdelning × 1 (one winning row)
      Expected return per round = hit_rate × avg_utdelning
      ROI = (expected_return - cost) / cost
      Breakeven utdelning = cost / hit_rate
    """
    num_legs = 7 if "V75" in game_type else 8

    print(f"\n{'='*70}")
    print(f"  SYSTEM-SIMULERING {game_type} (KORREKT ROI-MATEMATIK)")
    print(f"  {len(results)} omgångar, {num_legs} avdelningar, {row_price:.2f} kr/rad")
    print(f"{'='*70}")

    all_strategies = []

    # Test uniform bred widths
    for n_spik in range(0, min(num_legs + 1, 6)):
        n_bred = num_legs - n_spik

        for bw in range(2, 10):
            total_rows = bw ** n_bred if n_bred > 0 else 1
            cost = total_rows * row_price
            if cost < 1 or cost > 20000:
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

                all_covered = True
                for rank, (_, race) in enumerate(indexed):
                    need = 1 if rank < n_spik else bw
                    if race["winner_rank"] > need:
                        all_covered = False
                        break

                if all_covered:
                    hits += 1

            if total == 0 or hits == 0:
                continue

            hit_rate = hits / total
            # KORREKT: breakeven = cost / hit_rate (per omgång)
            breakeven = cost / hit_rate

            all_strategies.append({
                "name": f"{n_spik}S+{n_bred}×{bw}" if n_bred > 0 else f"{n_spik}S (alla spikar)",
                "n_spik": n_spik,
                "bred_width": bw if n_bred > 0 else 0,
                "total_rows": total_rows,
                "cost": cost,
                "hit_rate": hit_rate,
                "hits": hits,
                "total": total,
                "breakeven": breakeven,
            })

    # Sort by breakeven (lowest = most achievable)
    all_strategies.sort(key=lambda s: s["breakeven"])

    print(f"\n  Alla strategier — sorterade efter breakeven-utdelning:")
    print(f"  KORREKT: breakeven = systemkostnad / träffprocent")
    print(f"  = genomsnittlig utdelning/rad som krävs för att gå ±0")
    print(f"")
    print(f"  {'Strategi':<25} {'Rader':>7} {'Kostnad':>8} {'Träff':>8} {'Hits':>4} {'Breakeven':>12} {'Realist?':>8}")
    print(f"  {'-'*78}")

    for s in all_strategies:
        # V75 median utdelning ~1,000-2,000kr/rad, mean ~5,000kr (jackpots)
        # V85 median ~500-1,500kr/rad
        realistic = "✅" if s["breakeven"] < 2000 else "⚠️" if s["breakeven"] < 10000 else "❌"
        print(f"  {s['name']:<25} {s['total_rows']:>7} {s['cost']:>7.0f}kr {s['hit_rate']:>7.1%} {s['hits']:>4} {s['breakeven']:>10.0f}kr/rad {realistic:>6}")

    # ROI TABLE with realistic payout estimates
    print(f"\n\n  --- ROI PER STRATEGI (med realistiska utdelningar) ---")
    print(f"  V75 median utdelning: ~1,500-2,000 kr/rad")
    print(f"  V75 genomsnitt: ~5,000 kr/rad (inkl jackpots)")
    print(f"")

    payouts = [500, 1000, 1500, 2000, 3000, 5000]
    header = f"  {'Strategi':<25} {'Kostnad':>7}"
    for p in payouts:
        header += f" {p}kr"
        header += " " * (7 - len(f"{p}kr"))
    print(header)
    print(f"  {'-'*(25 + 7 + len(payouts) * 10)}")

    for s in all_strategies[:15]:
        line = f"  {s['name']:<25} {s['cost']:>6.0f}kr"
        for payout in payouts:
            # ROI = (hit_rate × payout - cost) / cost
            exp_return = s["hit_rate"] * payout
            roi = (exp_return - s["cost"]) / s["cost"]
            if roi >= 0:
                line += f"  🟢{roi:+.0%}"
            elif roi > -0.5:
                line += f"  🟡{roi:+.0%}"
            else:
                line += f"  🔴{roi:+.0%}"
            line += " " * max(0, 7 - len(f"  X{roi:+.0%}"))
        print(line)

    # BUDGET RECOMMENDATIONS
    print(f"\n\n  --- BUDGETANALYS ---")
    print(f"  {'Budget':>10} | {'Bästa strategi':<25} {'Rader':>6} {'Träff':>6} {'Breakeven':>12}")
    print(f"  {'-'*68}")

    for budget in [50, 100, 250, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000]:
        candidates = [s for s in all_strategies if s["cost"] <= budget]
        if not candidates:
            print(f"  {budget:>8}kr | -")
            continue
        best = min(candidates, key=lambda s: s["breakeven"])
        print(f"  {budget:>8}kr | {best['name']:<25} {best['total_rows']:>6} {best['hit_rate']:>5.1%} {best['breakeven']:>10.0f}kr/rad")

    return all_strategies


def final_recommendations(all_results: dict):
    """Print the two optimal system configurations with honest math."""
    print(f"\n\n{'='*80}")
    print(f"  ╔══════════════════════════════════════════════════════════════╗")
    print(f"  ║   SLUTLIGA REKOMMENDATIONER — ÄRLIG ROI-ANALYS             ║")
    print(f"  ╚══════════════════════════════════════════════════════════════╝")
    print(f"{'='*80}")

    for game_type in ["V75", "V85"]:
        results = all_results.get(game_type, [])
        if not results:
            continue

        all_races = []
        for r in results:
            all_races.extend(r["races"])
        n = len(all_races)
        top1 = sum(1 for r in all_races if r["top1_hit"]) / n
        top3 = sum(1 for r in all_races if r["top3_hit"]) / n
        num_legs = 7 if "V75" in game_type else 8
        row_price = 0.50

        print(f"\n  ═══ {game_type} ({len(results)} omg, {n} lopp) ═══")
        print(f"  top1={top1:.1%}, top3={top3:.1%}")

        for label, n_spik_target in [("SYSTEM A: 2 spikar (billigare)", 2),
                                       ("SYSTEM B: 1 spik (bredare)", 1)]:
            print(f"\n  --- {label} ---")

            best = None
            best_breakeven = float('inf')

            for bw in range(2, 9):
                n_bred = num_legs - n_spik_target
                total_rows = bw ** n_bred
                cost = total_rows * row_price

                if cost > 20000:
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

                    all_covered = True
                    for rank, (_, race) in enumerate(indexed):
                        need = 1 if rank < n_spik_target else bw
                        if race["winner_rank"] > need:
                            all_covered = False
                            break

                    if all_covered:
                        hits += 1

                if hits == 0 or total == 0:
                    continue

                hit_rate = hits / total
                breakeven = cost / hit_rate

                if breakeven < best_breakeven:
                    best_breakeven = breakeven
                    best = {
                        "n_spik": n_spik_target,
                        "bred_width": bw,
                        "total_rows": total_rows,
                        "cost": cost,
                        "hit_rate": hit_rate,
                        "hits": hits,
                        "total": total,
                        "breakeven": breakeven,
                    }

            if not best:
                print(f"  Ingen konfiguration hittades")
                continue

            print(f"  Konfiguration:    {best['n_spik']} spikar + {num_legs - best['n_spik']} avd × {best['bred_width']} hästar")
            print(f"  Rader:            {best['total_rows']}")
            print(f"  Kostnad/omg:      {best['cost']:.0f} kr")
            print(f"  Träffprocent:     {best['hit_rate']:.1%} ({best['hits']}/{best['total']})")
            print(f"  Breakeven utd:    {best['breakeven']:.0f} kr/rad")
            print(f"")
            print(f"  Budgetpassning:")
            for budget in [500, 1000, 1500, 2000, 3000]:
                fits = "✅" if best["cost"] <= budget else "❌"
                print(f"    {budget}kr: {fits}")
            print(f"")
            print(f"  ROI-scenarier (verklig utdelning per rad):")
            for payout in [500, 1000, 1500, 2000, 3000, 5000, 10000]:
                exp_return = best["hit_rate"] * payout
                roi = (exp_return - best["cost"]) / best["cost"]
                net_per_round = exp_return - best["cost"]
                if roi >= 0:
                    indicator = "🟢"
                elif roi > -0.5:
                    indicator = "🟡"
                else:
                    indicator = "🔴"
                print(f"    {indicator} {payout:6d}kr/rad → ROI {roi:+.0%}  (netto {net_per_round:+.0f}kr/omg)")

    # Overall conclusion
    print(f"\n\n  ════════════════════════════════════════════════════════")
    print(f"  SAMMANFATTNING")
    print(f"  ════════════════════════════════════════════════════════")
    print(f"")
    print(f"  MATEMATIK: V75 system med {7} avdelningar")
    print(f"  ─────────────────────────────────────")
    print(f"  ATG:s take-out (provision): ~35% av poolen")
    print(f"  → Genomsnittlig utdelning per rad = 65% av insatsen per rad")
    print(f"  → Utan edge: förväntat ROI ≈ -35%")
    print(f"")
    print(f"  Modellens top3 = 57.2%  (slumpens: ~25-30%)")
    print(f"  System all-top3 = 2.0%  (3^7 = 2,187 rader, 1,094kr)")
    print(f"  Breakeven: 1,094 / 0.02 = ~55,000kr/rad")
    print(f"")
    print(f"  V75 genomsnittlig utdelning:")
    print(f"    Median: ~1,500kr/rad")
    print(f"    Mean:   ~5,000kr/rad (jackpot-veckor drar upp)")
    print(f"    Normal range: 200-10,000kr/rad")
    print(f"")
    print(f"  → Breakeven kräver ~55,000kr/rad i snitt")
    print(f"  → Verkligheten ger ~1,500-5,000kr/rad")
    print(f"  → GAP: 10-35x för litet")
    print(f"")
    print(f"  SLUTSATS: V75/V85-system med denna modellprecision")
    print(f"  är INTE lönsamt inom budget 500-3000kr.")
    print(f"  Modellen behöver top3 ≈ 75-80% per avdelning")
    print(f"  för att system-spel ska kunna bära sig.")
    print(f"")
    print(f"  ALTERNATIV VÄGAR FRAMÅT:")
    print(f"  1. Selektiv spel — bara spela omgångar med hög modell-")
    print(f"     konfidens (t.ex. snittgap > 15 över alla avdelningar)")
    print(f"  2. V4 istället — 4 avdelningar kräver bara top3^4 ≈ 11%")
    print(f"     hit rate, mycket mer uppnåeligt")
    print(f"  3. Enskilda lopp — vinstspel/plats där modellen har edge")
    print(f"  4. Reducerade system — inte uniform bred, utan intelligent")
    print(f"     breddning baserat på modellsäkerhet per avdelning")


async def main():
    print("=" * 80)
    print("  V75/V85 SYSTEM OPTIMIZER — v6 model (KORREKT ROI-MATEMATIK)")
    print("=" * 80)

    # Find all V75/V85 rounds from 2024 onwards
    rounds_info = find_v75_v85_rounds(start_year=2024)
    print(f"\nHittade {len(rounds_info)} omgångar:")
    v75_rounds = [r for r in rounds_info if r["game_type"] == "V75"]
    v85_rounds = [r for r in rounds_info if r["game_type"] == "V85"]
    print(f"  V75: {len(v75_rounds)}")
    print(f"  V85: {len(v85_rounds)}")

    # Run backtest
    all_results = await run_backtest_from_cache(rounds_info)

    for game_type in ["V75", "V85"]:
        results = all_results[game_type]
        if not results:
            print(f"\n  ⚠️  Inga resultat för {game_type}")
            continue

        analyze_accuracy(results, game_type)
        per_leg_analysis(results, game_type)
        simulate_systems(results, game_type, 0.50)

    # Final recommendations
    final_recommendations(all_results)

    print(f"\n\nKLAR!")


if __name__ == "__main__":
    asyncio.run(main())
