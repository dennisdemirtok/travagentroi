"""Cross-Game-Type Analysis — Jämför strategiernas prestation per spelform.

Kör alla 6 strategier separat på V75, V85, V86, GS75, V64
och jämför ROI, vinstfrekvens och netto per spelform.

Identifierar vilka speltyper som ger bäst edge.
"""

import asyncio
import re
import sys
from datetime import date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.betting.system_generator import SystemGenerator
from roi_backtest_integrated import count_correct, _count_winning_rows_in_system


# ── 6 strategier ──
STRATEGIES = [
    ("I_streck_1st", "avg_upset_lt_40", 2500, 0, 0, 0, "I_streck_1st"),
    ("Q_dom_x_mktgap", "avg_upset_lt_40", 2500, 0, 0, 0, "Q_dom_x_mktgap"),
    ("D_market_gap", "avg_upset_lt_40", 2500, 0, 0, 0, "D_market_gap"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 1, 75, 12, "I_streck_spik1"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 2, 75, 12, "I_streck_spik2"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 3, 70, 10, "I_streck_spik3"),
]

GAME_TYPES = ["V75", "V85", "V86", "GS75", "V64"]
MIN_ROUNDS_FOR_STATS = 5


def _run_strategy_on_rounds(rounds, strategy_tuple):
    """Kör en strategi på en lista omgångar, returnera ROI-metrics."""
    strategy, filt, budget, max_spikes, spike_conf, spike_gap, label = strategy_tuple
    gen = SystemGenerator(
        budget=budget, strategy=strategy, selective_filter=filt,
        max_spikes=max_spikes, spike_conf_threshold=spike_conf,
        spike_score_gap=spike_gap,
    )

    total_cost = 0
    total_payout = 0
    rounds_played = 0
    full_hits = 0
    partial_hits = 0
    yearly = defaultdict(lambda: {"cost": 0, "payout": 0, "rounds": 0, "wins": 0, "full": 0})

    for gr in rounds:
        system = gen.generate(gr)
        if system.skip_round:
            continue

        rounds_played += 1
        race_results = [race.result_order for race in gr.races]
        num_races = len(gr.races)
        num_correct = count_correct(system.race_picks, race_results)
        payout_per_row = gr.dividends.get(num_correct, 0.0)
        winning_rows = 0
        total_pay = 0.0

        if payout_per_row > 0:
            winning_rows = _count_winning_rows_in_system(
                system.race_picks, race_results, num_correct
            )
            total_pay = payout_per_row * winning_rows

        cost = system.total_cost
        total_cost += cost
        total_payout += total_pay

        if num_correct == num_races and total_pay > 0:
            full_hits += 1
        elif total_pay > 0:
            partial_hits += 1

        y = yearly[str(gr.round_date)[:4]]
        y["cost"] += cost
        y["payout"] += total_pay
        y["rounds"] += 1
        if total_pay > 0:
            y["wins"] += 1
        if num_correct == num_races and total_pay > 0:
            y["full"] += 1

    roi = (total_payout - total_cost) / total_cost * 100 if total_cost > 0 else 0
    netto = total_payout - total_cost

    return {
        "label": label,
        "rounds_played": rounds_played,
        "full_hits": full_hits,
        "partial_hits": partial_hits,
        "total_cost": total_cost,
        "total_payout": total_payout,
        "roi": round(roi, 1),
        "netto": round(netto, 0),
        "yearly": {year: {
            "rounds": y["rounds"], "wins": y["wins"], "full": y["full"],
            "cost": y["cost"], "payout": y["payout"],
            "roi": round((y["payout"] - y["cost"]) / y["cost"] * 100 if y["cost"] > 0 else 0, 1),
            "netto": round(y["payout"] - y["cost"], 0),
        } for year, y in sorted(yearly.items())},
    }


async def main():
    client = ATGClient()
    analyzer = CompositeAnalyzer()
    cache_dir = Path("cache")

    # ── Hitta alla omgångar ──
    found = set()
    pattern = re.compile(r"(" + "|".join(GAME_TYPES) + r")_(\d{4}-\d{2}-\d{2})_")
    for f in cache_dir.iterdir():
        m = pattern.match(f.name)
        if m:
            found.add((m.group(1), m.group(2)))

    print(f"Hittade {len(found)} omgångar i cache")

    # ── Ladda och gruppera per speltyp ──
    rounds_by_type = defaultdict(list)

    for game_type, game_date in sorted(found):
        try:
            gr = await client.fetch_full_round(game_type, date.fromisoformat(game_date))
            if gr and gr.races and gr.dividends:
                rounds_by_type[game_type].append(gr)
        except Exception:
            continue

    total_loaded = sum(len(v) for v in rounds_by_type.values())
    print(f"Laddade {total_loaded} omgångar med utdelningsdata")
    for gt in sorted(rounds_by_type.keys()):
        dates = sorted(str(gr.round_date) for gr in rounds_by_type[gt])
        print(f"  {gt}: {len(rounds_by_type[gt])} omgångar ({dates[0]} → {dates[-1]})")

    # ── Analysera alla omgångar ──
    all_rounds = []
    for gt_rounds in rounds_by_type.values():
        all_rounds.extend(gt_rounds)

    for gr in all_rounds:
        analyzer.analyze_round(gr)
    print("Alla omgångar analyserade\n")

    # ── Huvudanalys: ROI per speltyp × strategi ──
    print(f"{'='*100}")
    print(f"   CROSS-GAME-TYPE ANALYSIS — ROI per spelform")
    print(f"{'='*100}\n")

    # Matris: speltyp → strategi → resultat
    results_matrix = {}

    for gt in sorted(rounds_by_type.keys()):
        gt_rounds = rounds_by_type[gt]
        if len(gt_rounds) < MIN_ROUNDS_FOR_STATS:
            print(f"  ⚠ {gt}: Bara {len(gt_rounds)} omgångar — för lite data (min {MIN_ROUNDS_FOR_STATS})")
            continue

        results_matrix[gt] = {}
        for strat in STRATEGIES:
            res = _run_strategy_on_rounds(gt_rounds, strat)
            results_matrix[gt][strat[-1]] = res

    if not results_matrix:
        print("  Ingen speltyp med tillräckligt data!")
        return

    # ── Kompakt ROI-matris ──
    print(f"\n  ROI-matris (% return on investment):")
    print()

    # Header
    gt_labels = list(results_matrix.keys())
    gt_header = " | ".join(f"{gt:>12}" for gt in gt_labels)
    print(f"  {'Strategi':<18} | {gt_header}")
    n_rounds_header = " | ".join(f"{'('+str(len(rounds_by_type[gt]))+' omg)':>12}" for gt in gt_labels)
    print(f"  {'':18} | {n_rounds_header}")
    print(f"  {'-'*18}-+-" + "-+-".join(f"{'-'*12}" for _ in gt_labels))

    for strat in STRATEGIES:
        label = strat[-1]
        values = []
        for gt in gt_labels:
            if gt in results_matrix and label in results_matrix[gt]:
                roi = results_matrix[gt][label]["roi"]
                values.append(f"{roi:>+11.1f}%")
            else:
                values.append(f"{'N/A':>12}")
        print(f"  {label:<18} | {' | '.join(values)}")

    # ── Netto-matris ──
    print(f"\n  Netto-matris (kr):")
    print()
    print(f"  {'Strategi':<18} | {gt_header}")
    print(f"  {'-'*18}-+-" + "-+-".join(f"{'-'*12}" for _ in gt_labels))

    for strat in STRATEGIES:
        label = strat[-1]
        values = []
        for gt in gt_labels:
            if gt in results_matrix and label in results_matrix[gt]:
                netto = results_matrix[gt][label]["netto"]
                values.append(f"{netto:>+11,.0f}")
            else:
                values.append(f"{'N/A':>12}")
        print(f"  {label:<18} | {' | '.join(values)}")

    # ── Detaljerat per speltyp ──
    for gt in gt_labels:
        gt_rounds = rounds_by_type[gt]
        num_races_typical = len(gt_rounds[0].races) if gt_rounds else "?"

        print(f"\n{'─'*100}")
        print(f"  {gt} — {len(gt_rounds)} omgångar, typiskt {num_races_typical} lopp/omgång")
        print(f"{'─'*100}")

        print(f"  {'Strategi':<18} | {'Omg':>4} | {'Full':>4} | {'Del':>4} | "
              f"{'Insats':>12} | {'Utdelning':>12} | {'ROI':>8} | {'Netto':>12}")
        print(f"  {'-'*18}-+-{'-'*4}-+-{'-'*4}-+-{'-'*4}-+-"
              f"{'-'*12}-+-{'-'*12}-+-{'-'*8}-+-{'-'*12}")

        for strat in STRATEGIES:
            label = strat[-1]
            r = results_matrix[gt][label]
            print(f"  {label:<18} | {r['rounds_played']:>4} | {r['full_hits']:>4} | "
                  f"{r['partial_hits']:>4} | {r['total_cost']:>11,.0f} | "
                  f"{r['total_payout']:>11,.0f} | {r['roi']:>+7.1f}% | {r['netto']:>+11,.0f}")

        # År-för-år för bästa strategi
        best_strat = max(results_matrix[gt].keys(), key=lambda s: results_matrix[gt][s]["roi"])
        best_res = results_matrix[gt][best_strat]
        if best_res["yearly"]:
            print(f"\n  Bästa strategi ({best_strat}) år-för-år:")
            for year, y in sorted(best_res["yearly"].items()):
                print(f"    {year}: {y['rounds']:>3} omg | Full: {y['full']:>2} | "
                      f"ROI: {y['roi']:>+7.1f}% | Netto: {y['netto']:>+12,.0f} kr")

    # ── Sammanfattning ──
    print(f"\n{'='*100}")
    print(f"  SAMMANFATTNING — Bästa speltyp per strategi")
    print(f"{'='*100}\n")

    for strat in STRATEGIES:
        label = strat[-1]
        best_gt = None
        best_roi = float("-inf")
        for gt in gt_labels:
            if label in results_matrix[gt]:
                roi = results_matrix[gt][label]["roi"]
                if roi > best_roi:
                    best_roi = roi
                    best_gt = gt

        if best_gt:
            n_rounds = results_matrix[best_gt][label]["rounds_played"]
            sig = "✅" if n_rounds >= 30 else "⚠️ (< 30 omg)"
            print(f"  {label:<18}: Bäst i {best_gt} (ROI {best_roi:+.1f}%, {n_rounds} omg) {sig}")

    # Bästa kombination
    print(f"\n  Bästa speltyp totalt (snitt-ROI över alla strategier):")
    for gt in gt_labels:
        rois = [results_matrix[gt][s[-1]]["roi"] for s in STRATEGIES]
        avg = sum(rois) / len(rois)
        n = len(rounds_by_type[gt])
        print(f"    {gt}: snitt-ROI {avg:+.1f}% ({n} omgångar)")

    # Varning för låg sample
    low_sample = [gt for gt in gt_labels if len(rounds_by_type[gt]) < 30]
    if low_sample:
        print(f"\n  ⚠️  Varning: {', '.join(low_sample)} har < 30 omgångar.")
        print(f"      Resultat är INTE statistiskt signifikanta.")

    # Supabase
    try:
        from trav_agent.database.client import is_configured
        if is_configured():
            from trav_agent.database.sync import sync_analysis_run
            results = {}
            for gt in gt_labels:
                results[gt] = {
                    label: {
                        "roi": results_matrix[gt][label]["roi"],
                        "netto": results_matrix[gt][label]["netto"],
                        "rounds": results_matrix[gt][label]["rounds_played"],
                        "full_hits": results_matrix[gt][label]["full_hits"],
                    }
                    for label in [s[-1] for s in STRATEGIES]
                }
            sync_analysis_run(
                run_type="game_type_analysis",
                parameters={
                    "game_types": gt_labels,
                    "strategies": [s[-1] for s in STRATEGIES],
                    "rounds_per_type": {gt: len(rounds_by_type[gt]) for gt in gt_labels},
                },
                results=results,
                num_rounds=total_loaded,
            )
            print("\n  Analysresultat sparade till Supabase")
    except Exception as e:
        print(f"\n  (Kunde inte spara till Supabase: {e})")

    print()


if __name__ == "__main__":
    asyncio.run(main())
