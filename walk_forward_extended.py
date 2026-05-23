"""Walk-Forward Extended — 3-periods test med stöd för alla speltyper.

Delar data i tre perioder:
  Far OOS: 2015-2020 (aldrig sedd under utveckling)
  Train:   2021-2023 (parametrarna tunade på denna period)
  Test:    2024-2025+ (out-of-sample)

Kör 6 strategier på varje period och jämför ROI.
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


# ── 6 strategier ──
STRATEGIES = [
    ("I_streck_1st", "avg_upset_lt_40", 2500, 0, 0, 0, "I_streck_1st"),
    ("Q_dom_x_mktgap", "avg_upset_lt_40", 2500, 0, 0, 0, "Q_dom_x_mktgap"),
    ("D_market_gap", "avg_upset_lt_40", 2500, 0, 0, 0, "D_market_gap"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 1, 75, 12, "I_streck_spik1"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 2, 75, 12, "I_streck_spik2"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 3, 70, 10, "I_streck_spik3"),
]

FAR_OOS_CUTOFF = date(2021, 1, 1)   # Far OOS: < 2021
TRAIN_CUTOFF = date(2024, 1, 1)      # Train: 2021-2023, Test: 2024+

# Alla speltyper att ladda
GAME_TYPES = ["V75", "V85", "V86", "GS75", "V64"]


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
    pattern = re.compile(r"(" + "|".join(GAME_TYPES) + r")_(\d{4}-\d{2}-\d{2})_")
    for f in cache_dir.iterdir():
        m = pattern.match(f.name)
        if m:
            found.add((m.group(1), m.group(2)))

    dates_to_load = sorted(found)
    game_type_counts = defaultdict(int)
    for gt, _ in dates_to_load:
        game_type_counts[gt] += 1

    print(f"Hittade {len(dates_to_load)} omgångar i cache:")
    for gt in sorted(game_type_counts.keys()):
        print(f"  {gt}: {game_type_counts[gt]}")

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

    # ── Dela i tre perioder ──
    far_oos_rounds = [gr for gr in rounds if gr.round_date < FAR_OOS_CUTOFF]
    train_rounds = [gr for gr in rounds if FAR_OOS_CUTOFF <= gr.round_date < TRAIN_CUTOFF]
    test_rounds = [gr for gr in rounds if gr.round_date >= TRAIN_CUTOFF]

    # Per speltyp
    far_oos_by_type = defaultdict(list)
    train_by_type = defaultdict(list)
    test_by_type = defaultdict(list)
    for gr in far_oos_rounds:
        far_oos_by_type[gr.game_type].append(gr)
    for gr in train_rounds:
        train_by_type[gr.game_type].append(gr)
    for gr in test_rounds:
        test_by_type[gr.game_type].append(gr)

    print(f"{'='*90}")
    print(f"   WALK-FORWARD EXTENDED — 3-periods test")
    print(f"{'='*90}")
    print(f"  Far OOS: < {FAR_OOS_CUTOFF}  ({len(far_oos_rounds)} omgångar)")
    print(f"  Train:   {FAR_OOS_CUTOFF} → {TRAIN_CUTOFF}  ({len(train_rounds)} omgångar)")
    print(f"  Test:    >= {TRAIN_CUTOFF} ({len(test_rounds)} omgångar)")

    for period_name, by_type in [("Far OOS", far_oos_by_type), ("Train", train_by_type), ("Test", test_by_type)]:
        if by_type:
            types_str = ", ".join(f"{gt}={len(rounds)}" for gt, rounds in sorted(by_type.items()))
            print(f"    {period_name} per typ: {types_str}")

    print(f"{'='*90}\n")

    # ── Kör strategier (alla rundor samlade) ──
    header = (f"  {'Strategi':<18} | {'Far OOS':>10} | {'Train':>10} | {'Test':>10} | "
              f"{'Shrink T→E':>10} | {'Shrink F→T':>10} | {'Bedömning'}")
    print(header)
    print(f"  {'-'*18}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}-+-{'-'*15}")

    all_far = {}
    all_train = {}
    all_test = {}

    for strat in STRATEGIES:
        label = strat[-1]

        far_res = _run_strategy_on_rounds(far_oos_rounds, strat) if far_oos_rounds else None
        train_res = _run_strategy_on_rounds(train_rounds, strat)
        test_res = _run_strategy_on_rounds(test_rounds, strat)

        all_far[label] = far_res
        all_train[label] = train_res
        all_test[label] = test_res

        t_roi = train_res["roi"]
        e_roi = test_res["roi"]
        f_roi = far_res["roi"] if far_res else None

        # Shrinkage Train→Test
        if t_roi > 0 and e_roi > 0:
            shrink_te = e_roi / t_roi
            shrink_te_str = f"{shrink_te:.2f}"
        elif t_roi > 0 and e_roi <= 0:
            shrink_te = 0
            shrink_te_str = "0.00"
        else:
            shrink_te = None
            shrink_te_str = "N/A"

        # Shrinkage Far→Train
        if f_roi is not None and f_roi > 0 and t_roi > 0:
            shrink_ft = f_roi / t_roi
            shrink_ft_str = f"{shrink_ft:.2f}"
        elif f_roi is not None:
            shrink_ft = 0 if t_roi > 0 else None
            shrink_ft_str = "0.00" if t_roi > 0 else "N/A"
        else:
            shrink_ft = None
            shrink_ft_str = "N/A"

        # Bedömning baserad på Train→Test shrinkage
        if shrink_te is not None:
            if shrink_te >= 0.75:
                verdict = "✅ OK"
            elif shrink_te >= 0.50:
                verdict = "⚠️  Moderat"
            elif shrink_te >= 0.25:
                verdict = "🚨 Allvarlig"
            else:
                verdict = "💀 Extrem"
        else:
            verdict = "❓ N/A"

        f_str = f"{f_roi:>+9.1f}%" if f_roi is not None else f"{'N/A':>10}"

        print(f"  {label:<18} | {f_str} | {t_roi:>+9.1f}% | {e_roi:>+9.1f}% | "
              f"{shrink_te_str:>10} | {shrink_ft_str:>10} | {verdict}")

    # ── Detaljer per strategi ──
    for strat in STRATEGIES:
        label = strat[-1]
        far_res = all_far.get(label)
        train_res = all_train[label]
        test_res = all_test[label]

        print(f"\n{'─'*90}")
        print(f"  Strategi: {label}")
        print(f"{'─'*90}")
        print(f"  {'Period':<10} | {'Omg':>4} | {'Full':>4} | {'Del':>4} | "
              f"{'Insats':>12} | {'Utdelning':>12} | {'ROI':>8} | {'Netto':>12}")
        print(f"  {'-'*10}-+-{'-'*4}-+-{'-'*4}-+-{'-'*4}-+-"
              f"{'-'*12}-+-{'-'*12}-+-{'-'*8}-+-{'-'*12}")

        # Far OOS
        if far_res:
            r = far_res
            print(f"  {'FAR OOS':<10} | {r['rounds_played']:>4} | {r['full_hits']:>4} | "
                  f"{r['partial_hits']:>4} | {r['total_cost']:>11,.0f} | "
                  f"{r['total_payout']:>11,.0f} | {r['roi']:>+7.1f}% | {r['netto']:>+11,.0f}")

        # Train
        r = train_res
        print(f"  {'TRAIN':<10} | {r['rounds_played']:>4} | {r['full_hits']:>4} | "
              f"{r['partial_hits']:>4} | {r['total_cost']:>11,.0f} | "
              f"{r['total_payout']:>11,.0f} | {r['roi']:>+7.1f}% | {r['netto']:>+11,.0f}")

        # Test
        r = test_res
        print(f"  {'TEST':<10} | {r['rounds_played']:>4} | {r['full_hits']:>4} | "
              f"{r['partial_hits']:>4} | {r['total_cost']:>11,.0f} | "
              f"{r['total_payout']:>11,.0f} | {r['roi']:>+7.1f}% | {r['netto']:>+11,.0f}")

        # År för år
        print(f"\n  År-för-år:")
        all_yearly = {}
        for period_name, res in [("far_oos", far_res), ("train", train_res), ("test", test_res)]:
            if res:
                for year, data in res["yearly"].items():
                    all_yearly[year] = data

        for year in sorted(all_yearly.keys()):
            y = all_yearly[year]
            if int(year) < FAR_OOS_CUTOFF.year:
                marker = "  "
            elif int(year) < TRAIN_CUTOFF.year:
                marker = "  "
            else:
                marker = "» "

            # Gränslinjer
            boundary = ""
            if year == str(FAR_OOS_CUTOFF.year) and str(int(year) - 1) in all_yearly:
                boundary = f"\n  {'─'*4} train-gräns {'─'*50}\n"
            elif year == str(TRAIN_CUTOFF.year) and str(int(year) - 1) in all_yearly:
                boundary = f"\n  {'─'*4} test-gräns {'─'*50}\n"

            print(f"{boundary}  {marker}{year}: {y['rounds']:>3} omg | "
                  f"Full: {y['full']:>2} | Del: {y['wins'] - y['full']:>2} | "
                  f"ROI: {y['roi']:>+7.1f}% | Netto: {y['netto']:>+12,.0f} kr")

    # ── Per speltyp (om flera finns) ──
    all_types_in_data = set()
    for gr in rounds:
        all_types_in_data.add(gr.game_type)

    if len(all_types_in_data) > 1:
        print(f"\n\n{'='*90}")
        print(f"   PER SPELTYP")
        print(f"{'='*90}")

        for gt in sorted(all_types_in_data):
            gt_rounds = [gr for gr in rounds if gr.game_type == gt]
            if len(gt_rounds) < 5:
                print(f"\n  {gt}: Bara {len(gt_rounds)} omgångar — hoppar över")
                continue

            print(f"\n  {gt} ({len(gt_rounds)} omgångar)")
            print(f"  {'Strategi':<18} | {'ROI':>10} | {'Netto':>12} | {'Omg':>4} | {'Full':>4}")
            print(f"  {'-'*18}-+-{'-'*10}-+-{'-'*12}-+-{'-'*4}-+-{'-'*4}")

            for strat in STRATEGIES:
                res = _run_strategy_on_rounds(gt_rounds, strat)
                roi_str = f"{res['roi']:>+9.1f}%"
                print(f"  {res['label']:<18} | {roi_str} | {res['netto']:>+11,.0f} | "
                      f"{res['rounds_played']:>4} | {res['full_hits']:>4}")

    # ── Sammanfattning ──
    print(f"\n{'='*90}")
    print(f"  SAMMANFATTNING")
    print(f"{'='*90}")
    print(f"\n  Shrinkage-guide:")
    print(f"    > 0.75  = Låg curvefit-risk")
    print(f"    0.50-0.75 = Moderat curvefit")
    print(f"    0.25-0.50 = Allvarlig curvefit")
    print(f"    < 0.25  = Extrem curvefit")

    # Snittresultat
    train_rois = [all_train[s[-1]]["roi"] for s in STRATEGIES if all_train[s[-1]]["roi"] > 0]
    test_rois = [all_test[s[-1]]["roi"] for s in STRATEGIES if all_test[s[-1]]["roi"] > 0]
    if train_rois and test_rois:
        avg_train = sum(train_rois) / len(train_rois)
        avg_test = sum(test_rois) / len(test_rois)
        avg_shrink = avg_test / avg_train if avg_train > 0 else 0
        print(f"\n  Genomsnittlig shrinkage (Train→Test): {avg_shrink:.2f}")
        print(f"  (Train snitt-ROI: {avg_train:+.1f}% → Test snitt-ROI: {avg_test:+.1f}%)")

    far_rois = [all_far[s[-1]]["roi"] for s in STRATEGIES
                if all_far.get(s[-1]) and all_far[s[-1]]["roi"] > 0]
    if far_rois and train_rois:
        avg_far = sum(far_rois) / len(far_rois)
        avg_shrink_far = avg_far / avg_train if avg_train > 0 else 0
        print(f"\n  Genomsnittlig shrinkage (Far OOS vs Train): {avg_shrink_far:.2f}")
        print(f"  (Far OOS snitt-ROI: {avg_far:+.1f}%)")

    # Supabase
    try:
        from trav_agent.database.client import is_configured
        if is_configured():
            from trav_agent.database.sync import sync_analysis_run
            results = {}
            for strat in STRATEGIES:
                label = strat[-1]
                results[label] = {
                    "far_oos": all_far.get(label, {}),
                    "train": all_train[label],
                    "test": all_test[label],
                }
            sync_analysis_run(
                run_type="walk_forward_extended",
                parameters={
                    "far_oos_cutoff": str(FAR_OOS_CUTOFF),
                    "train_cutoff": str(TRAIN_CUTOFF),
                    "far_oos_rounds": len(far_oos_rounds),
                    "train_rounds": len(train_rounds),
                    "test_rounds": len(test_rounds),
                    "game_types": list(all_types_in_data),
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
