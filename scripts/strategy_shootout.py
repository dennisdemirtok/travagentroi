"""Strategy Shootout — Ren no-leakage jämförelse av ALLA strategier.

Testar varje strategi på V75+V85 data med:
1. Temporal filtrering av past_starts (inga framtida starter)
2. Karriär-omberäkning från filtrerade starter
3. Closing odds neutraliserade (behåller streckprocent)
4. Faktiska ATG-utdelningar (dividends)

Kör:
    python scripts/strategy_shootout.py
    python scripts/strategy_shootout.py --from 2025-01-01
    python scripts/strategy_shootout.py --type V85 --from 2025-06-01
"""

import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path
from collections import defaultdict

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.betting.system_generator import SystemGenerator
from trav_agent.analysis.system_builder import build_system
from trav_agent.backtest.runner import BacktestRunner
from trav_agent.config import ROW_PRICES


def count_correct(system_picks, race_results):
    """Räkna antal rätt i ett system."""
    correct = 0
    for rp, result in zip(system_picks, race_results):
        if not result:
            continue
        winner = result[0]
        if hasattr(rp, 'picks'):
            picks = rp.picks
        elif hasattr(rp, 'horses'):
            picks = [h.number for h in rp.horses]
        else:
            continue
        if winner in picks:
            correct += 1
    return correct


def count_winning_rows(system_picks, race_results, target_correct):
    """Räkna vinnande rader (multiplicera alla rätta loppens alternativ)."""
    if target_correct == 0:
        return 0

    correct_race_picks = []
    for rp, result in zip(system_picks, race_results):
        if not result:
            continue
        winner = result[0]
        if hasattr(rp, 'picks'):
            picks = rp.picks
        elif hasattr(rp, 'horses'):
            picks = [h.number for h in rp.horses]
        else:
            continue
        if winner in picks:
            correct_race_picks.append(1)  # Winner was in our picks
        else:
            if hasattr(rp, 'num_picks'):
                correct_race_picks.append(0)
            elif hasattr(rp, 'picks'):
                correct_race_picks.append(0)
            else:
                correct_race_picks.append(0)

    if len(correct_race_picks) < target_correct:
        return 0

    # For full-hit systems, winning rows = product of picks in incorrect races
    # For partial hits, it's more complex
    # Simple approach: if all correct, winning_rows = product of 1's for correct races
    # × picks in wrong races... actually for ATG payouts, winning_rows should be:
    # number of rows where exactly target_correct races are correct

    # For simplicity with ATG dividends: if we have the winner in all races,
    # winning_rows = 1 (the dividend is per winning row)
    # For partial: winning_rows = product of picks in wrong races

    winning_rows = 1
    for rp, result in zip(system_picks, race_results):
        if not result:
            continue
        winner = result[0]
        if hasattr(rp, 'picks'):
            picks = rp.picks
            n_picks = len(picks)
        elif hasattr(rp, 'horses'):
            picks = [h.number for h in rp.horses]
            n_picks = len(picks)
        else:
            continue
        if winner not in picks:
            winning_rows *= n_picks

    return winning_rows


def process_system_generator(gr, strategy, budget, cache_dir):
    """Process round with SystemGenerator strategy (A_union, B_marknad, C_bred, D_smart)."""
    gen = SystemGenerator(budget=budget, strategy=strategy)
    system = gen.generate(gr)

    if system.skip_round or not system.race_picks:
        return None

    race_results = [race.result_order for race in gr.races]
    num_races = len(gr.races)
    num_correct = count_correct(system.race_picks, race_results)

    payout_per_row = gr.dividends.get(num_correct, 0.0)
    winning_rows = 0
    total_payout = 0.0

    if payout_per_row > 0:
        winning_rows = count_winning_rows(system.race_picks, race_results, num_correct)
        total_payout = payout_per_row * winning_rows

    return {
        "date": str(gr.round_date),
        "game_type": gr.game_type,
        "cost": system.total_cost,
        "rows": system.total_rows,
        "num_correct": num_correct,
        "num_races": num_races,
        "hit": num_correct == num_races,
        "payout": total_payout,
        "payout_per_row": payout_per_row,
        "winning_rows": winning_rows,
    }


def process_system_builder(gr, strategy, budget):
    """Process round with system_builder strategy (optimal, chansspik, greedy)."""
    try:
        plan = build_system(gr, budget=budget, strategy=strategy)
    except Exception as e:
        return None

    if not plan.legs:
        return None

    race_results = [race.result_order for race in gr.races]
    num_races = len(gr.races)

    # Count correct: check each leg
    num_correct = 0
    for leg, result in zip(plan.legs, race_results):
        if not result:
            continue
        winner = result[0]
        if winner in leg.picks:
            num_correct += 1

    payout_per_row = gr.dividends.get(num_correct, 0.0)
    total_payout = 0.0
    winning_rows = 0

    if payout_per_row > 0:
        # Count winning rows: product of picks in WRONG races
        winning_rows = 1
        for leg, result in zip(plan.legs, race_results):
            if not result:
                continue
            winner = result[0]
            if winner not in leg.picks:
                winning_rows *= len(leg.picks)
        total_payout = payout_per_row * winning_rows

    return {
        "date": str(gr.round_date),
        "game_type": gr.game_type,
        "cost": plan.total_cost,
        "rows": plan.total_rows,
        "num_correct": num_correct,
        "num_races": num_races,
        "hit": num_correct == num_races,
        "payout": total_payout,
        "payout_per_row": payout_per_row,
        "winning_rows": winning_rows,
    }


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Strategy Shootout — no-leakage backtest")
    parser.add_argument("--from", dest="from_date", default="2025-01-01")
    parser.add_argument("--to", dest="to_date", default="2026-12-31")
    parser.add_argument("--type", dest="types", action="append", default=None)
    args = parser.parse_args()

    game_types = args.types or ["V75", "V85", "GS75"]
    from_date = args.from_date
    to_date = args.to_date

    client = ATGClient()
    analyzer = CompositeAnalyzer()
    cache_dir = Path(PROJECT_ROOT / "cache")

    # ── Find cached rounds ──
    all_dates = defaultdict(set)
    for f in cache_dir.iterdir():
        for gt in game_types:
            m = re.match(rf"^{gt}_(\d{{4}}-\d{{2}}-\d{{2}})_", f.name)
            if m:
                d = m.group(1)
                if from_date <= d <= to_date:
                    all_dates[gt].add(d)

    dates_to_load = []
    for gt in game_types:
        for d in sorted(all_dates[gt]):
            dates_to_load.append((gt, d))

    print(f"╔══════════════════════════════════════════════════════════╗")
    print(f"║  STRATEGY SHOOTOUT — No-Leakage Backtest               ║")
    print(f"╠══════════════════════════════════════════════════════════╣")
    print(f"║  Period: {from_date} → {to_date}")
    for gt in game_types:
        print(f"║  {gt}: {len(all_dates[gt])} omgångar")
    print(f"║  Totalt: {len(dates_to_load)} omgångar")
    print(f"╚══════════════════════════════════════════════════════════╝\n")

    # ── Load rounds ──
    rounds = []
    failed = 0
    for game_type, game_date in dates_to_load:
        try:
            gr = await client.fetch_full_round(game_type, date.fromisoformat(game_date))
            if gr and gr.races and gr.is_finished:
                rounds.append(gr)
        except Exception as e:
            failed += 1
            continue

    print(f"Loaded {len(rounds)} finished rounds ({failed} failed)\n")

    # ── LEAKAGE PREVENTION ──
    print("Applying leakage prevention...")
    for gr in rounds:
        # 1. Remove future past_starts + recompute career
        BacktestRunner._filter_future_starts(gr)
        # 2. Neutralize closing odds (keep betDistribution/streckprocent)
        BacktestRunner._neutralize_closing_odds(gr)

    # ── Run analysis ──
    print("Running CompositeAnalyzer on all rounds...")
    for gr in rounds:
        analyzer.analyze_round(gr)
    print(f"Analysis complete ({len(rounds)} rounds)\n")

    # ── Define strategies to test ──
    strategies = []

    # SystemGenerator strategies at various budgets
    for strat in ["A_union", "B_marknad", "C_bred", "D_smart"]:
        for budget in [500, 1000, 2000]:
            strategies.append({
                "label": f"{strat}_{budget}",
                "type": "system_generator",
                "strategy": strat,
                "budget": budget,
            })

    # system_builder strategies
    for strat in ["optimal", "chansspik", "greedy"]:
        for budget in [300, 500, 1000]:
            strategies.append({
                "label": f"SB_{strat}_{budget}",
                "type": "system_builder",
                "strategy": strat,
                "budget": budget,
            })

    # ── Run all strategies ──
    results = {}

    for s in strategies:
        label = s["label"]
        entries = []

        for gr in rounds:
            if s["type"] == "system_generator":
                entry = process_system_generator(gr, s["strategy"], s["budget"], cache_dir)
            else:
                entry = process_system_builder(gr, s["strategy"], s["budget"])

            if entry:
                entries.append(entry)

        # Aggregate
        total_cost = sum(e["cost"] for e in entries)
        total_payout = sum(e["payout"] for e in entries)
        full_hits = sum(1 for e in entries if e["hit"])
        partial_hits = sum(1 for e in entries if e["payout"] > 0 and not e["hit"])
        roi = (total_payout - total_cost) / total_cost * 100 if total_cost > 0 else 0
        netto = total_payout - total_cost
        avg_cost = total_cost / len(entries) if entries else 0

        results[label] = {
            "label": label,
            "rounds": len(entries),
            "full_hits": full_hits,
            "partial_hits": partial_hits,
            "total_hits": full_hits + partial_hits,
            "total_cost": total_cost,
            "total_payout": total_payout,
            "roi": roi,
            "netto": netto,
            "avg_cost": avg_cost,
            "hit_rate": full_hits / len(entries) * 100 if entries else 0,
            "entries": entries,
        }

    # ── Print results sorted by ROI ──
    print("=" * 100)
    print(f"{'STRATEGI':<25} {'OMG':>5} {'HITS':>6} {'DEL':>5} {'HIT%':>7} "
          f"{'KOSTNAD':>12} {'UTBET':>12} {'NETTO':>12} {'ROI':>8} {'SNI.KOST':>10}")
    print("=" * 100)

    sorted_results = sorted(results.values(), key=lambda r: r["roi"], reverse=True)

    for r in sorted_results:
        marker = " ★" if r["roi"] > 0 else ""
        print(f"{r['label']:<25} {r['rounds']:>5} {r['full_hits']:>6} {r['partial_hits']:>5} "
              f"{r['hit_rate']:>6.1f}% "
              f"{r['total_cost']:>11,.0f} {r['total_payout']:>11,.0f} "
              f"{r['netto']:>+11,.0f} {r['roi']:>+7.1f}%{marker} "
              f"{r['avg_cost']:>9,.0f}")

    print("=" * 100)

    # ── Top performers ──
    profitable = [r for r in sorted_results if r["roi"] > 0]
    print(f"\n{'★ LÖNSAMMA STRATEGIER':}")
    print(f"  {len(profitable)} av {len(sorted_results)} strategier är lönsamma\n")

    if profitable:
        print(f"  TOPP 5:")
        for i, r in enumerate(profitable[:5], 1):
            print(f"  {i}. {r['label']:25s} ROI: {r['roi']:+.1f}%  Netto: {r['netto']:+,.0f} kr  "
                  f"Hits: {r['full_hits']}/{r['rounds']}  Snittkostnad: {r['avg_cost']:,.0f} kr")

    # ── Grupperad analys per strategityp ──
    print(f"\n{'─' * 80}")
    print("ANALYS PER STRATEGITYP (bästa budget per typ):\n")

    type_groups = defaultdict(list)
    for r in sorted_results:
        # Extract base strategy name
        parts = r["label"].split("_")
        if parts[0] == "SB":
            base = f"SB_{parts[1]}"
        else:
            base = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else parts[0]
        type_groups[base].append(r)

    for base_name in sorted(type_groups.keys()):
        group = type_groups[base_name]
        best = max(group, key=lambda r: r["roi"])
        print(f"  {base_name:20s} → Bäst: {best['label']:25s} ROI: {best['roi']:+.1f}%  "
              f"Netto: {best['netto']:+,.0f} kr")
        for r in sorted(group, key=lambda x: x["roi"], reverse=True):
            if r != best:
                print(f"  {'':20s}         {r['label']:25s} ROI: {r['roi']:+.1f}%  "
                      f"Netto: {r['netto']:+,.0f} kr")

    # ── Save detailed results ──
    output_file = PROJECT_ROOT / "strategy_shootout_results.json"
    save_data = {}
    for label, r in results.items():
        save_data[label] = {k: v for k, v in r.items() if k != "entries"}
        save_data[label]["per_round"] = [
            {"date": e["date"], "game_type": e["game_type"],
             "cost": e["cost"], "rows": e["rows"],
             "correct": e["num_correct"], "races": e["num_races"],
             "hit": e["hit"], "payout": e["payout"]}
            for e in r["entries"]
        ]

    with open(output_file, "w") as f:
        json.dump(save_data, f, indent=2, ensure_ascii=False)
    print(f"\nDetaljerade resultat sparade: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
