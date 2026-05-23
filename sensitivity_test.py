"""Sensitivity Analysis — Mät parameterkänslighet för curvefit-bedömning.

Varierar nyckelparametrar och mäter hur ROI förändras.
Om ROI kollapsar vid ±20% = parameter är curvefit.
"""

import asyncio
import re
import sys
import statistics
from copy import deepcopy
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.betting.system_generator import SystemGenerator
from trav_agent.config import AnalysisConfig, FactorWeights
from roi_backtest_integrated import count_correct, _count_winning_rows_in_system


# ── Strategier ──
STRATEGIES = [
    ("I_streck_1st", "avg_upset_lt_40", 2500, 0, 0, 0, "I_streck_1st"),
    ("Q_dom_x_mktgap", "avg_upset_lt_40", 2500, 0, 0, 0, "Q_dom_x_mktgap"),
    ("D_market_gap", "avg_upset_lt_40", 2500, 0, 0, 0, "D_market_gap"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 1, 75, 12, "I_streck_spik1"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 2, 75, 12, "I_streck_spik2"),
    ("I_streck_1st", "avg_upset_lt_40", 2500, 3, 70, 10, "I_streck_spik3"),
]

# ── Parametrar att testa ──
# (param_name, [test_values], param_category, default_value)
PARAM_SWEEPS = [
    # Category A: AnalysisConfig
    ("super_score_model_weight", [0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75], "config", 0.55),
    ("spike_min_score", [65.0, 70.0, 75.0, 80.0, 85.0, 90.0], "config", 80.0),
    ("spike_min_gap", [4.0, 6.0, 8.0, 10.0, 12.0, 15.0], "config", 8.0),

    # Category B: FactorWeights (multiplikator)
    ("post_position", [0.6, 0.8, 1.0, 1.2, 1.4], "weight", 1.0),
    ("category_profile", [0.6, 0.8, 1.0, 1.2, 1.4], "weight", 1.0),
    ("prize_index", [0.6, 0.8, 1.0, 1.2, 1.4], "weight", 1.0),
    ("time_analysis", [0.6, 0.8, 1.0, 1.2, 1.4], "weight", 1.0),

    # Category C: SystemGenerator
    ("budget", [1000, 1500, 2000, 2500, 3000, 3500, 4000], "sysgen", 2500),
    ("spike_conf_threshold", [60, 65, 70, 75, 80, 85, 90], "sysgen", 75),
    ("spike_score_gap", [6, 8, 10, 12, 14, 16, 18], "sysgen", 12),
]


def _run_pipeline(rounds, config, strategy_overrides=None):
    """Kör hela pipelinen: analysera + generera system + beräkna ROI.

    Args:
        rounds: Lista med GameRound-objekt
        config: AnalysisConfig att använda
        strategy_overrides: Dict med parameter-override per strategi (valfritt)

    Returns:
        Dict med ROI per strategilabel
    """
    analyzer = CompositeAnalyzer(config=config)
    for gr in rounds:
        analyzer.analyze_round(gr)

    results = {}
    for strat_tuple in STRATEGIES:
        strategy, filt, budget, max_spikes, spike_conf, spike_gap, label = strat_tuple

        # Applicera eventuella overrides
        if strategy_overrides:
            budget = strategy_overrides.get("budget", budget)
            # Spike-parametrar bara för spik-strategin
            if label.startswith("I_streck_spik"):
                spike_conf = strategy_overrides.get("spike_conf_threshold", spike_conf)
                spike_gap = strategy_overrides.get("spike_score_gap", spike_gap)

        gen = SystemGenerator(
            budget=budget, strategy=strategy, selective_filter=filt,
            max_spikes=max_spikes, spike_conf_threshold=spike_conf,
            spike_score_gap=spike_gap,
        )

        total_cost = 0
        total_payout = 0
        rounds_played = 0

        for gr in rounds:
            system = gen.generate(gr)
            if system.skip_round:
                continue

            rounds_played += 1
            race_results = [race.result_order for race in gr.races]
            num_races = len(gr.races)
            num_correct = count_correct(system.race_picks, race_results)
            payout_per_row = gr.dividends.get(num_correct, 0.0)

            cost = system.total_cost
            total_cost += cost

            if payout_per_row > 0:
                winning_rows = _count_winning_rows_in_system(
                    system.race_picks, race_results, num_correct
                )
                total_payout += payout_per_row * winning_rows

        roi = (total_payout - total_cost) / total_cost * 100 if total_cost > 0 else 0
        results[label] = {
            "roi": round(roi, 1),
            "netto": round(total_payout - total_cost, 0),
            "rounds": rounds_played,
            "cost": total_cost,
            "payout": total_payout,
        }

    return results


async def main():
    client = ATGClient()
    cache_dir = Path("cache")

    # ── Ladda omgångar ──
    found = set()
    for f in cache_dir.iterdir():
        m = re.match(r"(V75|V85)_(\d{4}-\d{2}-\d{2})_", f.name)
        if m:
            found.add((m.group(1), m.group(2)))

    print(f"Hittade {len(found)} omgångar i cache")

    rounds = []
    for game_type, game_date in sorted(found):
        try:
            gr = await client.fetch_full_round(game_type, date.fromisoformat(game_date))
            if gr and gr.races and gr.dividends:
                rounds.append(gr)
        except Exception:
            continue

    print(f"Laddade {len(rounds)} omgångar med utdelningsdata\n")

    # ── Baseline ──
    print(f"{'='*80}")
    print(f"   SENSITIVITY ANALYSIS — Parameterkänslighet")
    print(f"{'='*80}\n")

    default_config = AnalysisConfig()
    baseline = _run_pipeline(rounds, default_config)

    print(f"  BASELINE (nuvarande parametrar):")
    for label in [s[-1] for s in STRATEGIES]:
        r = baseline[label]
        print(f"    {label:<18}: ROI {r['roi']:>+8.1f}% | Netto {r['netto']:>+12,.0f} kr | {r['rounds']} omg")
    print()

    # ── Parametersweep ──
    cliff_results = []

    for param_name, test_values, category, default_val in PARAM_SWEEPS:
        print(f"{'─'*80}")
        if category == "weight":
            print(f"  Factor-vikt: {param_name} (default={getattr(FactorWeights(), param_name)}, multiplikator)")
        elif category == "config":
            print(f"  Config: {param_name} (default={default_val})")
        else:
            print(f"  SystemGenerator: {param_name} (default={default_val})")
        print(f"{'─'*80}")

        # Header
        labels = ["I_streck", "Q_dom", "D_mkt", "Spik1", "Spik2", "Spik3"]
        header = "  " + f"{'Värde':>8} | " + " | ".join(f"{l:>10}" for l in labels)
        print(header)
        print(f"  {'-'*8}-+-" + "-+-".join(f"{'-'*10}" for _ in labels))

        all_rois = {s[-1]: [] for s in STRATEGIES}

        for val in test_values:
            is_default = (val == default_val)

            if category == "config":
                cfg = AnalysisConfig()
                setattr(cfg, param_name, val)
                result = _run_pipeline(rounds, cfg)
            elif category == "weight":
                weights = FactorWeights()
                original = getattr(weights, param_name)
                setattr(weights, param_name, original * val)
                cfg = AnalysisConfig(weights=weights)
                result = _run_pipeline(rounds, cfg)
            elif category == "sysgen":
                # SystemGenerator-parametrar: kör med default config men override sysgen params
                overrides = {param_name: val}
                result = _run_pipeline(rounds, default_config, strategy_overrides=overrides)
            else:
                continue

            marker = "*" if is_default else " "
            roi_strs = []
            for strat in STRATEGIES:
                label = strat[-1]
                r = result[label]["roi"]
                all_rois[label].append(r)
                roi_strs.append(f"{r:>+8.1f}%")

            if category == "weight":
                actual_weight = getattr(FactorWeights(), param_name) * val
                val_str = f"{val:.1f}×({actual_weight:.2f})"
                print(f"  {val_str:>8}{marker}| {' | '.join(roi_strs)}")
            else:
                print(f"  {val:>8}{marker}| {' | '.join(roi_strs)}")

        # Stabilitet
        for strat in STRATEGIES:
            label = strat[-1]
            rois = all_rois[label]
            if len(rois) >= 2:
                stddev = statistics.stdev(rois)
                baseline_roi = baseline[label]["roi"]
                max_drop = min(rois) - baseline_roi
                max_rise = max(rois) - baseline_roi

                # Cliff detection per strategi
                if label == "I_streck_1st":  # Använd primär strategi för cliff
                    if abs(max_drop) > abs(baseline_roi) * 0.5 and baseline_roi > 0:
                        verdict = "🚨 CLIFF"
                    elif abs(max_drop) > abs(baseline_roi) * 0.3 and baseline_roi > 0:
                        verdict = "⚠️  Känslig"
                    else:
                        verdict = "✅ Stabil"

                    cliff_results.append({
                        "param": param_name,
                        "category": category,
                        "stddev": round(stddev, 1),
                        "max_drop": round(max_drop, 1),
                        "verdict": verdict,
                    })

        # Print stability line for primary strategy
        primary_rois = all_rois["I_streck_1st"]
        if len(primary_rois) >= 2:
            stddev = statistics.stdev(primary_rois)
            baseline_roi = baseline["I_streck_1st"]["roi"]
            max_drop = min(primary_rois) - baseline_roi
            print(f"\n  I_streck stabilitet: StdDev={stddev:.1f}%, Max drop={max_drop:+.1f}%")

        print()

    # ── Cliff Detection Sammanfattning ──
    print(f"\n{'='*80}")
    print(f"   CLIFF DETECTION — Sammanfattning (baserad på I_streck_1st)")
    print(f"{'='*80}\n")

    print(f"  {'Parameter':<28} | {'Kategori':<10} | {'StdDev':>8} | {'Max drop':>10} | {'Bedömning'}")
    print(f"  {'-'*28}-+-{'-'*10}-+-{'-'*8}-+-{'-'*10}-+-{'-'*15}")

    for cr in cliff_results:
        cat_str = {"config": "Config", "weight": "Vikt", "sysgen": "SysGen"}[cr["category"]]
        print(f"  {cr['param']:<28} | {cat_str:<10} | {cr['stddev']:>7.1f}% | "
              f"{cr['max_drop']:>+9.1f}% | {cr['verdict']}")

    # Övergripande bedömning
    cliffs = sum(1 for cr in cliff_results if "CLIFF" in cr["verdict"])
    sensitive = sum(1 for cr in cliff_results if "Känslig" in cr["verdict"])
    stable = sum(1 for cr in cliff_results if "Stabil" in cr["verdict"])

    print(f"\n  Totalt: {stable} stabil, {sensitive} känslig, {cliffs} cliff")

    if cliffs >= 3:
        print(f"  🚨 HÖG curvefitting-risk — många parametrar är cliff-känsliga")
    elif cliffs >= 1 or sensitive >= 3:
        print(f"  ⚠️  MODERAT curvefitting-risk — viss parameterkänslighet")
    else:
        print(f"  ✅ LÅG curvefitting-risk — modellen är robust mot parameterförändringar")

    # Supabase
    try:
        from trav_agent.database.client import is_configured
        if is_configured():
            from trav_agent.database.sync import sync_analysis_run
            sync_analysis_run(
                run_type="sensitivity",
                parameters={
                    "param_sweeps": [(p, v, c, d) for p, v, c, d in PARAM_SWEEPS],
                    "strategies": [s[-1] for s in STRATEGIES],
                },
                results={
                    "cliff_results": cliff_results,
                    "baseline": {label: baseline[label] for label in [s[-1] for s in STRATEGIES]},
                },
                summary=f"{stable} stabil, {sensitive} känslig, {cliffs} cliff",
                num_rounds=len(rounds),
            )
            print("\n  Analysresultat sparade till Supabase")
    except Exception as e:
        print(f"\n  (Kunde inte spara till Supabase: {e})")

    print()


if __name__ == "__main__":
    asyncio.run(main())
