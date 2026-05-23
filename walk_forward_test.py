"""Walk-Forward Overfitting Test — Mät curvefit i backtesterna.

Delar data i:
  Train: 2021-2023 (parametrarna tunade på denna period)
  Test:  2024-2025+ (out-of-sample)

Kör 4 strategier på varje subset och jämför ROI.
Shrinkage ratio = test_ROI / train_ROI → mäter curvefit.
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


# ── Samma 4 strategier som generate_backlog.py ──
STRATEGIES = [
    ("I_streck_1st", "avg_upset_lt_40", 2500, 0, 0, 0, "I_streck_1st"),
    ("Q_dom_x_mktgap", "avg_upset_lt_40", 2500, 0, 0, 0, "Q_dom_x_mktgap"),
    ("D_market_gap", "avg_upset_lt_40", 2500, 0, 0, 0, "D_market_gap"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 1, 75, 12, "I_streck_spik1"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 2, 75, 12, "I_streck_spik2"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 3, 70, 10, "I_streck_spik3"),
]

TRAIN_CUTOFF = date(2024, 1, 1)


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

    yearly_data = {}
    for year in sorted(yearly.keys()):
        y = yearly[year]
        y_roi = (y["payout"] - y["cost"]) / y["cost"] * 100 if y["cost"] > 0 else 0
        yearly_data[year] = {
            "rounds": y["rounds"], "wins": y["wins"], "full": y["full"],
            "cost": y["cost"], "payout": y["payout"],
            "roi": round(y_roi, 1), "netto": round(y["payout"] - y["cost"], 0),
        }

    return {
        "label": label,
        "rounds_played": rounds_played,
        "full_hits": full_hits,
        "partial_hits": partial_hits,
        "total_cost": total_cost,
        "total_payout": total_payout,
        "roi": round(roi, 1),
        "netto": round(netto, 0),
        "yearly": yearly_data,
    }


async def main():
    client = ATGClient()
    analyzer = CompositeAnalyzer()
    cache_dir = Path("cache")

    # ── Hitta alla omgångar ──
    found = set()
    for f in cache_dir.iterdir():
        m = re.match(r"(V75|V85)_(\d{4}-\d{2}-\d{2})_", f.name)
        if m:
            found.add((m.group(1), m.group(2)))

    dates_to_load = sorted(found)
    print(f"Hittade {len(dates_to_load)} omgångar i cache")

    # ── Ladda omgångar ──
    rounds = []
    for game_type, game_date in dates_to_load:
        try:
            gr = await client.fetch_full_round(game_type, date.fromisoformat(game_date))
            if gr and gr.races and gr.dividends:
                rounds.append(gr)
        except Exception:
            continue

    print(f"Laddade {len(rounds)} omgångar med utdelningsdata")

    # ── Analysera alla ──
    for gr in rounds:
        analyzer.analyze_round(gr)
    print("Alla omgångar analyserade\n")

    # ── Dela i train / test ──
    train_rounds = [gr for gr in rounds if gr.round_date < TRAIN_CUTOFF]
    test_rounds = [gr for gr in rounds if gr.round_date >= TRAIN_CUTOFF]

    # Separera V75 och V85 i test
    test_v75 = [gr for gr in test_rounds if gr.game_type == "V75"]
    test_v85 = [gr for gr in test_rounds if gr.game_type == "V85"]

    print(f"{'='*70}")
    print(f"   WALK-FORWARD OVERFITTING TEST")
    print(f"{'='*70}")
    print(f"  Train: < {TRAIN_CUTOFF}  ({len(train_rounds)} omgångar)")
    print(f"  Test:  >= {TRAIN_CUTOFF} ({len(test_rounds)} omgångar, "
          f"V75={len(test_v75)}, V85={len(test_v85)})")
    print(f"{'='*70}\n")

    # ── Kör strategier ──
    header = f"  {'Strategi':<18} | {'Train ROI':>10} | {'Test ROI':>10} | {'Shrinkage':>10} | {'Bedömning'}"
    print(header)
    print(f"  {'-'*18}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}-+-{'-'*15}")

    all_train = {}
    all_test = {}

    for strat in STRATEGIES:
        label = strat[-1]
        train_res = _run_strategy_on_rounds(train_rounds, strat)
        test_res = _run_strategy_on_rounds(test_rounds, strat)
        all_train[label] = train_res
        all_test[label] = test_res

        t_roi = train_res["roi"]
        e_roi = test_res["roi"]

        if t_roi > 0 and e_roi > 0:
            shrinkage = e_roi / t_roi
            shrinkage_str = f"{shrinkage:.2f}"
        elif t_roi > 0 and e_roi <= 0:
            shrinkage = 0
            shrinkage_str = "0.00"
        else:
            shrinkage = None
            shrinkage_str = "N/A"

        if shrinkage is not None:
            if shrinkage >= 0.75:
                verdict = "✅ OK"
            elif shrinkage >= 0.50:
                verdict = "⚠️  Moderat"
            elif shrinkage >= 0.25:
                verdict = "🚨 Allvarlig"
            else:
                verdict = "💀 Extrem"
        else:
            verdict = "❓ N/A"

        print(f"  {label:<18} | {t_roi:>+9.1f}% | {e_roi:>+9.1f}% | {shrinkage_str:>10} | {verdict}")

    # ── Detaljer per strategi ──
    for strat in STRATEGIES:
        label = strat[-1]
        train_res = all_train[label]
        test_res = all_test[label]

        print(f"\n{'─'*70}")
        print(f"  Strategi: {label}")
        print(f"{'─'*70}")
        print(f"  {'Period':<8} | {'Omg':>4} | {'Full':>4} | {'Del':>4} | "
              f"{'Insats':>12} | {'Utdelning':>12} | {'ROI':>8} | {'Netto':>12}")
        print(f"  {'-'*8}-+-{'-'*4}-+-{'-'*4}-+-{'-'*4}-+-"
              f"{'-'*12}-+-{'-'*12}-+-{'-'*8}-+-{'-'*12}")

        # Train
        r = train_res
        print(f"  {'TRAIN':<8} | {r['rounds_played']:>4} | {r['full_hits']:>4} | "
              f"{r['partial_hits']:>4} | {r['total_cost']:>11,.0f} | "
              f"{r['total_payout']:>11,.0f} | {r['roi']:>+7.1f}% | {r['netto']:>+11,.0f}")

        # Test
        r = test_res
        print(f"  {'TEST':<8} | {r['rounds_played']:>4} | {r['full_hits']:>4} | "
              f"{r['partial_hits']:>4} | {r['total_cost']:>11,.0f} | "
              f"{r['total_payout']:>11,.0f} | {r['roi']:>+7.1f}% | {r['netto']:>+11,.0f}")

        # År för år
        print(f"\n  År-för-år:")
        all_years = sorted(set(list(train_res["yearly"].keys()) + list(test_res["yearly"].keys())))
        for year in all_years:
            y = train_res["yearly"].get(year) or test_res["yearly"].get(year)
            if not y:
                continue
            marker = "  " if int(year) < TRAIN_CUTOFF.year else "» "
            boundary = ""
            if year == str(TRAIN_CUTOFF.year) and str(int(year) - 1) in train_res["yearly"]:
                boundary = f"\n  {'─'*4} test-gräns {'─'*40}\n"
            print(f"{boundary}  {marker}{year}: {y['rounds']:>3} omg | "
                  f"Full: {y['full']:>2} | Del: {y['wins'] - y['full']:>2} | "
                  f"ROI: {y['roi']:>+7.1f}% | Netto: {y['netto']:>+12,.0f} kr")

    # ── Sammanfattning ──
    print(f"\n{'='*70}")
    print(f"  SAMMANFATTNING")
    print(f"{'='*70}")
    print(f"\n  Shrinkage-guide:")
    print(f"    > 0.75  = Låg curvefit-risk (parametrarna generaliserar bra)")
    print(f"    0.50-0.75 = Moderat curvefit (förväntad ROI-reduktion ~30-50%)")
    print(f"    0.25-0.50 = Allvarlig curvefit (ROI troligen kraftigt överskattad)")
    print(f"    < 0.25  = Extrem curvefit (backtestet är i princip meningslöst)")

    # Snittresultat
    train_rois = [all_train[s[-1]]["roi"] for s in STRATEGIES if all_train[s[-1]]["roi"] > 0]
    test_rois = [all_test[s[-1]]["roi"] for s in STRATEGIES if all_test[s[-1]]["roi"] > 0]
    if train_rois and test_rois:
        avg_train = sum(train_rois) / len(train_rois)
        avg_test = sum(test_rois) / len(test_rois)
        avg_shrink = avg_test / avg_train if avg_train > 0 else 0
        print(f"\n  Genomsnittlig shrinkage: {avg_shrink:.2f}")
        print(f"  (Train snitt-ROI: {avg_train:+.1f}% → Test snitt-ROI: {avg_test:+.1f}%)")

    # Supabase
    try:
        from trav_agent.database.client import is_configured
        if is_configured():
            from trav_agent.database.sync import sync_analysis_run
            results = {}
            for strat in STRATEGIES:
                label = strat[-1]
                results[label] = {
                    "train": all_train[label],
                    "test": all_test[label],
                }
            sync_analysis_run(
                run_type="walk_forward",
                parameters={
                    "train_cutoff": str(TRAIN_CUTOFF),
                    "train_rounds": len(train_rounds),
                    "test_rounds": len(test_rounds),
                },
                results=results,
                num_rounds=len(rounds),
            )
            print("\n  Analysresultat sparade till Supabase")
    except Exception as e:
        print(f"\n  (Kunde inte spara till Supabase: {e})")

    print()


if __name__ == "__main__":
    asyncio.run(main())
