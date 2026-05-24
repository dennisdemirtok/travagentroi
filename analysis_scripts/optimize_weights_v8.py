#!/usr/bin/env python3
"""Optimera faktorvikter v8 — med 14 faktorer på 1559 lopp.

Testar:
1. REN MODELL: vilka vikter maximerar rank-1 utan marknad?
2. HYBRID: vilka vikter maximerar rank-1 med 20% modell + 80% marknad?
3. Vilka faktorer ska BORT? (nollvikt → bättre?)

Metod: Random search + hill climbing (samma som v7.1 men ny data).
"""

import json
import random
import statistics
from collections import defaultdict
from pathlib import Path


def load_races():
    """Ladda alla 1559 lopp med faktorpoäng + marknad."""
    files = [
        "backtest_results/backtest_V75_2024-01-01_2026-04-01.json",
        "backtest_results/backtest_V85_2024-01-01_2026-04-01.json",
        "backtest_results/backtest_V86_2024-01-01_2026-04-01.json",
    ]
    races = []
    for fp in files:
        p = Path(fp)
        if not p.exists():
            continue
        with open(p) as f:
            data = json.load(f)
        for rd in data["predictions"]:
            for race in rd["races"]:
                actual = race.get("actual_result", [])
                factors = race.get("horse_factor_scores", {})
                bet_pct = race.get("horse_bet_pct", {})
                if not actual or not factors:
                    continue
                winner = actual[0]
                if str(winner) not in factors:
                    continue
                races.append({
                    "winner": winner,
                    "factors": factors,
                    "bet_pct": bet_pct,
                })
    return races


FACTOR_NAMES = [
    "time_analysis", "prize_index", "age", "post_position",
    "competition_strength", "track_profile", "driver_class",
    "category_profile", "last_win", "form_curve", "gallop_risk",
    "equipment", "driver_trainer", "layoff",
]


def compute_pure_model_ranking(race, weights):
    """Beräkna ren modell-ranking med givna vikter."""
    horses = {}
    for h_str, fs in race["factors"].items():
        h = int(h_str)
        score = sum(weights.get(fn, 0) * fs.get(fn, 50.0) for fn in FACTOR_NAMES)
        horses[h] = score
    return sorted(horses.keys(), key=lambda h: horses[h], reverse=True)


def compute_hybrid_ranking(race, weights, model_weight=0.20):
    """Beräkna hybrid-ranking med givna vikter + marknad."""
    market_weight = 1.0 - model_weight
    bet_pct = race["bet_pct"]

    # Model scores
    model_scores = {}
    for h_str, fs in race["factors"].items():
        h = int(h_str)
        model_scores[h] = sum(weights.get(fn, 0) * fs.get(fn, 50.0) for fn in FACTOR_NAMES)

    # Market scores (normalized)
    market_scores = {}
    bp_vals = {int(k): float(v) for k, v in bet_pct.items() if float(v) > 0}
    if not bp_vals:
        return sorted(model_scores.keys(), key=lambda h: model_scores[h], reverse=True)

    bp_min = min(bp_vals.values())
    bp_max = max(bp_vals.values())
    bp_spread = bp_max - bp_min if bp_max > bp_min else 1.0
    for h, bp in bp_vals.items():
        market_scores[h] = 5.0 + 90.0 * (bp - bp_min) / bp_spread

    # Normalize model scores
    ms_vals = list(model_scores.values())
    ms_min, ms_max = min(ms_vals), max(ms_vals)
    ms_spread = ms_max - ms_min if ms_max > ms_min else 1.0

    # Hybrid
    common = set(model_scores.keys()) & set(market_scores.keys())
    hybrid = {}
    for h in common:
        m_norm = 5.0 + 90.0 * (model_scores[h] - ms_min) / ms_spread
        hybrid[h] = model_weight * m_norm + market_weight * market_scores[h]

    # Add non-common horses
    for h in set(model_scores.keys()) - common:
        hybrid[h] = model_scores[h]

    return sorted(hybrid.keys(), key=lambda h: hybrid.get(h, 0), reverse=True)


def evaluate(races, weights, use_hybrid=True, model_weight=0.20):
    """Beräkna rank-1, top-4, avg rank med givna vikter."""
    r1 = t4 = 0
    total = 0
    rank_sum = 0

    for race in races:
        if use_hybrid:
            ranking = compute_hybrid_ranking(race, weights, model_weight)
        else:
            ranking = compute_pure_model_ranking(race, weights)

        winner = race["winner"]
        if winner not in ranking:
            continue
        pos = ranking.index(winner) + 1
        total += 1
        rank_sum += pos
        if pos == 1:
            r1 += 1
        if pos <= 4:
            t4 += 1

    if total == 0:
        return 0, 0, 99

    return r1 / total * 100, t4 / total * 100, rank_sum / total


def normalize_weights(weights):
    """Normalisera vikter till summa 1.0."""
    total = sum(weights.values())
    if total == 0:
        return {k: 1.0 / len(weights) for k in weights}
    return {k: v / total for k, v in weights.items()}


def random_weights():
    """Generera slumpmässiga vikter."""
    w = {fn: random.random() for fn in FACTOR_NAMES}
    return normalize_weights(w)


def mutate_weights(weights, strength=0.1):
    """Mutera vikter med given styrka."""
    w = dict(weights)
    # Ändra 2-4 faktorer
    n_changes = random.randint(2, 4)
    targets = random.sample(FACTOR_NAMES, n_changes)
    for fn in targets:
        delta = random.gauss(0, strength)
        w[fn] = max(0.0, w[fn] + delta)
    return normalize_weights(w)


def main():
    races = load_races()
    print(f"Lopp: {len(races)}")

    # Nuvarande vikter (v7.1)
    current_weights = normalize_weights({
        "post_position": 0.157, "prize_index": 0.156, "age": 0.146,
        "time_analysis": 0.130, "competition_strength": 0.102,
        "track_profile": 0.074, "driver_class": 0.062,
        "category_profile": 0.062, "last_win": 0.049,
        "form_curve": 0.049, "gallop_risk": 0.035,
        "equipment": 0.005, "driver_trainer": 0.005, "layoff": 0.003,
    })

    # ── 1. Nuvarande vikter — ren modell ──
    print()
    print("=" * 70)
    print("1. NUVARANDE VIKTER (v7.1)")
    print("=" * 70)

    r1_m, t4_m, avg_m = evaluate(races, current_weights, use_hybrid=False)
    r1_h, t4_h, avg_h = evaluate(races, current_weights, use_hybrid=True)
    print(f"\n  Ren modell:     Rank 1: {r1_m:.1f}%, Top 4: {t4_m:.1f}%, Avg: {avg_m:.2f}")
    print(f"  Hybrid (20/80): Rank 1: {r1_h:.1f}%, Top 4: {t4_h:.1f}%, Avg: {avg_h:.2f}")

    # ── 2. Test utan brusfaktorer ──
    print()
    print("=" * 70)
    print("2. UTAN BRUSFAKTORER (equipment, driver_trainer, layoff)")
    print("=" * 70)

    no_noise = dict(current_weights)
    for fn in ["equipment", "driver_trainer", "layoff"]:
        no_noise[fn] = 0.0
    no_noise = normalize_weights(no_noise)

    r1_m2, t4_m2, avg_m2 = evaluate(races, no_noise, use_hybrid=False)
    r1_h2, t4_h2, avg_h2 = evaluate(races, no_noise, use_hybrid=True)
    print(f"\n  Ren modell:     Rank 1: {r1_m2:.1f}% ({r1_m2-r1_m:+.1f}%), Top 4: {t4_m2:.1f}%")
    print(f"  Hybrid (20/80): Rank 1: {r1_h2:.1f}% ({r1_h2-r1_h:+.1f}%), Top 4: {t4_h2:.1f}%")

    # ── 3. Test med bara toppfaktorer ──
    print()
    print("=" * 70)
    print("3. BARA TOPP-6 FAKTORER")
    print("=" * 70)

    top6 = {fn: 0.0 for fn in FACTOR_NAMES}
    for fn in ["prize_index", "age", "time_analysis", "category_profile", "driver_class", "post_position"]:
        top6[fn] = current_weights[fn]
    top6 = normalize_weights(top6)

    r1_m3, t4_m3, avg_m3 = evaluate(races, top6, use_hybrid=False)
    r1_h3, t4_h3, avg_h3 = evaluate(races, top6, use_hybrid=True)
    print(f"\n  Ren modell:     Rank 1: {r1_m3:.1f}% ({r1_m3-r1_m:+.1f}%), Top 4: {t4_m3:.1f}%")
    print(f"  Hybrid (20/80): Rank 1: {r1_h3:.1f}% ({r1_h3-r1_h:+.1f}%), Top 4: {t4_h3:.1f}%")

    # ── 4. Random search + hill climbing ──
    print()
    print("=" * 70)
    print("4. VIKTSOPTIMERING (random search + hill climbing)")
    print("=" * 70)

    # Optimize for HYBRID rank-1 (since that's what we actually use)
    best_weights = dict(current_weights)
    best_r1 = r1_h
    best_t4 = t4_h

    # Random search: 2000 random weight sets
    print(f"\n  Random search (2000 iterationer)...")
    for i in range(2000):
        w = random_weights()
        r1, t4, avg = evaluate(races, w, use_hybrid=True)
        if r1 > best_r1 or (r1 == best_r1 and t4 > best_t4):
            best_r1 = r1
            best_t4 = t4
            best_weights = w
            if i % 100 == 0:
                print(f"    [{i}] Ny bästa: {r1:.1f}% rank-1, {t4:.1f}% top-4")

    print(f"  Bästa efter random: {best_r1:.1f}% rank-1, {best_t4:.1f}% top-4")

    # Hill climbing: mutera bästa vikter
    print(f"\n  Hill climbing (1000 iterationer)...")
    no_improve = 0
    for i in range(1000):
        strength = 0.05 if no_improve < 50 else 0.10 if no_improve < 100 else 0.20
        w = mutate_weights(best_weights, strength)
        r1, t4, avg = evaluate(races, w, use_hybrid=True)
        if r1 > best_r1 or (r1 == best_r1 and t4 > best_t4):
            best_r1 = r1
            best_t4 = t4
            best_weights = w
            no_improve = 0
            print(f"    [{i}] Förbättrad: {r1:.1f}% rank-1, {t4:.1f}% top-4")
        else:
            no_improve += 1

    print(f"\n  Bästa efter hill climbing: {best_r1:.1f}% rank-1, {best_t4:.1f}% top-4")

    # ── 5. Resultat ──
    print()
    print("=" * 70)
    print("5. OPTIMALA VIKTER")
    print("=" * 70)

    # Also evaluate pure model with best weights
    r1_pm, t4_pm, avg_pm = evaluate(races, best_weights, use_hybrid=False)

    print(f"\n  {'Faktor':<25} {'Nuvarande':>10} {'Optimerad':>10} {'Ändring':>10}")
    print(f"  {'─'*25} {'─'*10} {'─'*10} {'─'*10}")

    for fn in sorted(FACTOR_NAMES, key=lambda f: best_weights.get(f, 0), reverse=True):
        cur = current_weights.get(fn, 0) * 100
        opt = best_weights.get(fn, 0) * 100
        diff = opt - cur
        marker = "↑" if diff > 1 else "↓" if diff < -1 else "="
        print(f"  {fn:<25} {cur:>9.1f}% {opt:>9.1f}% {diff:>+9.1f}% {marker}")

    print(f"\n  RESULTAT:")
    print(f"    Ren modell (nuvarande):  {r1_m:.1f}% → {r1_pm:.1f}% ({r1_pm-r1_m:+.1f}%)")
    print(f"    Hybrid (nuvarande):      {r1_h:.1f}% → {best_r1:.1f}% ({best_r1-r1_h:+.1f}%)")

    # Test different model weights with optimized factors
    print()
    print("=" * 70)
    print("6. OPTIMAL MODELL-VIKT MED NYA FAKTORER")
    print("=" * 70)
    print(f"\n  {'Modell%':>8} │ {'Rank 1':>8} {'Top 4':>8} {'Avg':>8}")
    print(f"  {'─'*8} │ {'─'*8} {'─'*8} {'─'*8}")

    for mw_pct in [0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 100]:
        mw = mw_pct / 100.0
        r1, t4, avg = evaluate(races, best_weights, use_hybrid=True, model_weight=mw)
        marker = " ★" if r1 >= best_r1 else ""
        print(f"  {mw_pct:>7}% │ {r1:>7.1f}% {t4:>7.1f}% {avg:>7.2f}{marker}")

    # Print Python code for config.py
    print()
    print("=" * 70)
    print("PYTHON CODE FÖR config.py")
    print("=" * 70)
    print()
    for fn in sorted(FACTOR_NAMES, key=lambda f: best_weights.get(f, 0), reverse=True):
        val = best_weights[fn]
        print(f"    {fn}: float = {val:.3f}")


if __name__ == "__main__":
    main()
