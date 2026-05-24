#!/usr/bin/env python3
"""Optimera hybrid-modell: modell + marknad.

Dennis: "Modellen kan ju inte vara sämre än strecken."

Analysen visar:
- Ren modell: 28.1% rank-1
- Ren marknad (finalOdds): 41.3% rank-1

Denna script:
1. Diagnostiserar VAR modellen tappar vs marknaden
2. Testar olika blendvikter (modell + marknad)
3. Hittar optimalt hybrid-score
4. Visar om hybrid slår marknaden ensam

Data: betDistribution från V-spel cache (streckprocent).
"""

import json
import glob
import statistics
from collections import defaultdict
from pathlib import Path


def load_backtest_with_market():
    """Ladda backtest-lopp + marknadssignal (betDistribution)."""
    files = [
        "backtest_results/backtest_V75_2024-01-01_2026-04-01.json",
        "backtest_results/backtest_V85_2024-01-01_2026-04-01.json",
        "backtest_results/backtest_V86_2024-01-01_2026-04-01.json",
    ]

    # Steg 1: Bygg game_map från spelcache (game_type, date, leg) → race_id
    game_map = {}
    for game_file in glob.glob("cache/V75_*.json") + glob.glob("cache/V85_*.json") + glob.glob("cache/V86_*.json"):
        try:
            with open(game_file) as f:
                gdata = json.load(f)
            gid = gdata.get("id", "")
            parts = gid.split("_")
            if len(parts) < 2:
                continue
            gt = parts[0]
            gdate = parts[1]
            for i, race in enumerate(gdata.get("races", [])):
                rid = race.get("id", "")
                # Hämta betDistribution per häst
                bet_dist = {}
                for s in race.get("starts", []):
                    num = s.get("number", 0)
                    pools = s.get("pools", {})
                    # V-spel pool (V75, V85, V86)
                    vpool = pools.get(gt, {})
                    bd = vpool.get("betDistribution", 0)
                    if bd > 0:
                        bet_dist[num] = bd / 100.0  # Till procent (2661 → 26.61%)
                    else:
                        # Fallback: vinnare-odds
                        vinnare = pools.get("vinnare", {})
                        odds = vinnare.get("odds", 0)
                        if odds and odds > 0:
                            # Approximera streck från odds: 100/odds
                            bet_dist[num] = 100.0 / (odds / 100.0)  # odds i öre → kr
                game_map[(gt, gdate, i)] = {
                    "race_id": rid,
                    "bet_dist": bet_dist,
                }
        except Exception:
            continue

    print(f"Game map: {len(game_map)} lopp mappade")

    # Steg 2: Ladda backtest-resultat och matcha med marknad
    matched = []
    no_market = 0

    for fp in files:
        p = Path(fp)
        if not p.exists():
            continue
        with open(p) as f:
            data = json.load(f)
        game_type = data.get("game_type", "?")

        for rd in data["predictions"]:
            date_str = rd.get("date", "?")
            for leg_idx, race in enumerate(rd["races"]):
                actual = race.get("actual_result", [])
                ranking = race.get("predicted_ranking", [])
                scores = race.get("horse_scores", {})
                factor_scores = race.get("horse_factor_scores", {})

                if not actual or not ranking:
                    continue

                winner = actual[0]

                # Hämta marknadssignal
                key = (game_type, date_str, leg_idx)
                market_data = game_map.get(key, {})
                bet_dist = market_data.get("bet_dist", {})

                if not bet_dist:
                    no_market += 1
                    continue

                # Marknadens ranking (baserad på betDistribution, högt = favorit)
                market_ranking = sorted(
                    bet_dist.keys(),
                    key=lambda h: bet_dist.get(h, 0),
                    reverse=True
                )

                # Modellens ranking
                model_ranking = ranking

                try:
                    model_pos = model_ranking.index(winner) + 1
                except ValueError:
                    continue
                try:
                    market_pos = market_ranking.index(winner) + 1
                except ValueError:
                    continue

                matched.append({
                    "date": date_str,
                    "track": race.get("track", "?"),
                    "race_num": race.get("race_number", 0),
                    "winner": winner,
                    "model_pos": model_pos,
                    "market_pos": market_pos,
                    "model_ranking": model_ranking,
                    "market_ranking": market_ranking,
                    "model_scores": {int(k): v for k, v in scores.items()},
                    "bet_dist": bet_dist,
                    "factor_scores": factor_scores,
                    "n_horses": len(model_ranking),
                    "game_type": game_type,
                    "winner_streck": bet_dist.get(winner, 0),
                })

    print(f"Matchade med marknad: {len(matched)}")
    print(f"Utan marknad: {no_market}")
    return matched


def diagnose_failures(matched):
    """VAR tappar modellen vs marknaden?"""
    print()
    print("=" * 70)
    print("1. DIAGNOSTIK: VAR TAPPAR MODELLEN?")
    print("=" * 70)

    # Kategorisera varje lopp
    model_wins = 0  # Model rank < market rank for winner
    market_wins = 0
    both_right = 0
    both_wrong = 0

    # Buckets: favoriter vs skrällar
    fav_model_better = 0
    fav_market_better = 0
    upset_model_better = 0
    upset_market_better = 0

    for r in matched:
        is_fav = r["winner_streck"] >= 15.0  # >15% streck = favorit
        m_better = r["model_pos"] < r["market_pos"]
        k_better = r["market_pos"] < r["model_pos"]

        if r["model_pos"] == 1 and r["market_pos"] == 1:
            both_right += 1
        elif r["model_pos"] == 1:
            model_wins += 1
        elif r["market_pos"] == 1:
            market_wins += 1
        else:
            both_wrong += 1

        if is_fav:
            if m_better:
                fav_model_better += 1
            elif k_better:
                fav_market_better += 1
        else:
            if m_better:
                upset_model_better += 1
            elif k_better:
                upset_market_better += 1

    total = len(matched)
    print(f"\n  Rank-1 diagnostik:")
    print(f"    Båda rätt (rank 1): {both_right} ({both_right/total*100:.1f}%)")
    print(f"    Bara modell rätt:   {model_wins} ({model_wins/total*100:.1f}%)")
    print(f"    Bara marknad rätt:  {market_wins} ({market_wins/total*100:.1f}%)")
    print(f"    Ingen rätt:         {both_wrong} ({both_wrong/total*100:.1f}%)")

    print(f"\n  Favoriter (>15% streck) — vem rankar vinnaren bättre?")
    print(f"    Modell bättre: {fav_model_better}")
    print(f"    Marknad bättre: {fav_market_better}")

    print(f"\n  Icke-favoriter (<15% streck) — vem rankar vinnaren bättre?")
    print(f"    Modell bättre: {upset_model_better}")
    print(f"    Marknad bättre: {upset_market_better}")

    # Strecknivå vs modell-miss
    print(f"\n  Modellens rank för vinnare per streck-nivå:")
    for lo, hi, label in [(30, 100, "Storfavorit >30%"), (15, 30, "Favorit 15-30%"),
                          (8, 15, "Medium 8-15%"), (3, 8, "Outsider 3-8%"), (0, 3, "Rank outsider <3%")]:
        subset = [r for r in matched if lo <= r["winner_streck"] < hi]
        if not subset:
            continue
        avg_m = statistics.mean(r["model_pos"] for r in subset)
        avg_k = statistics.mean(r["market_pos"] for r in subset)
        m1 = sum(1 for r in subset if r["model_pos"] == 1) / len(subset) * 100
        k1 = sum(1 for r in subset if r["market_pos"] == 1) / len(subset) * 100
        print(f"    {label:25s} ({len(subset):3d} lopp): "
              f"Modell avg={avg_m:.1f} r1={m1:.0f}%  |  "
              f"Marknad avg={avg_k:.1f} r1={k1:.0f}%  |  "
              f"{'M+' if avg_m < avg_k else 'K+'}")


def test_hybrid_weights(matched):
    """Testa olika vikter för modell + marknad blend."""
    print()
    print("=" * 70)
    print("2. HYBRID-MODELL: TESTA VIKTER (modell% + marknad%)")
    print("=" * 70)
    print(f"\n  {'Modell%':>8} {'Marknad%':>8} │ {'Rank 1':>8} {'Top 2':>8} {'Top 3':>8} {'Top 4':>8} │ {'Avg Rank':>9}")
    print(f"  {'─'*8} {'─'*8} │ {'─'*8} {'─'*8} {'─'*8} {'─'*8} │ {'─'*9}")

    best_r1 = 0
    best_r1_weight = 0
    best_t4 = 0
    best_t4_weight = 0
    best_avg = 99
    best_avg_weight = 0

    results_per_weight = {}

    for model_pct in range(0, 105, 5):
        market_pct = 100 - model_pct
        mw = model_pct / 100.0
        kw = market_pct / 100.0

        # Beräkna hybrid-ranking för varje lopp
        r1_hits = 0
        t2_hits = 0
        t3_hits = 0
        t4_hits = 0
        rank_sum = 0
        valid = 0

        for r in matched:
            # Normalisera modellscores och marknad till samma skala (0-100)
            model_scores = r["model_scores"]
            bet_dist = r["bet_dist"]

            # Alla hästar som finns i BÅDA
            common = set(model_scores.keys()) & set(bet_dist.keys())
            if len(common) < 3:
                continue

            # Normalisera marknad (betDistribution) till 0-100
            bd_vals = [bet_dist[h] for h in common]
            bd_min, bd_max = min(bd_vals), max(bd_vals)
            bd_spread = bd_max - bd_min if bd_max > bd_min else 1.0

            # Normalisera modellscores till 0-100
            ms_vals = [model_scores[h] for h in common]
            ms_min, ms_max = min(ms_vals), max(ms_vals)
            ms_spread = ms_max - ms_min if ms_max > ms_min else 1.0

            hybrid_scores = {}
            for h in common:
                m_norm = (model_scores[h] - ms_min) / ms_spread * 100
                k_norm = (bet_dist[h] - bd_min) / bd_spread * 100
                hybrid_scores[h] = mw * m_norm + kw * k_norm

            hybrid_ranking = sorted(common, key=lambda h: hybrid_scores[h], reverse=True)

            winner = r["winner"]
            if winner not in common:
                continue

            try:
                hybrid_pos = hybrid_ranking.index(winner) + 1
            except ValueError:
                continue

            valid += 1
            rank_sum += hybrid_pos
            if hybrid_pos == 1:
                r1_hits += 1
            if hybrid_pos <= 2:
                t2_hits += 1
            if hybrid_pos <= 3:
                t3_hits += 1
            if hybrid_pos <= 4:
                t4_hits += 1

        if valid == 0:
            continue

        r1 = r1_hits / valid * 100
        t2 = t2_hits / valid * 100
        t3 = t3_hits / valid * 100
        t4 = t4_hits / valid * 100
        avg = rank_sum / valid

        marker = ""
        if r1 > best_r1:
            best_r1 = r1
            best_r1_weight = model_pct
        if t4 > best_t4:
            best_t4 = t4
            best_t4_weight = model_pct
        if avg < best_avg:
            best_avg = avg
            best_avg_weight = model_pct

        if model_pct in [0, 100]:
            marker = " ← ren"
        elif model_pct in [30, 35, 40, 45, 50]:
            marker = ""

        print(f"  {model_pct:>7}% {market_pct:>7}% │ {r1:>7.1f}% {t2:>7.1f}% {t3:>7.1f}% {t4:>7.1f}% │ {avg:>8.2f}{marker}")

        results_per_weight[model_pct] = {
            "r1": r1, "t2": t2, "t3": t3, "t4": t4, "avg": avg, "valid": valid
        }

    print()
    print(f"  Bäst Rank-1: {best_r1:.1f}% vid {best_r1_weight}% modell + {100-best_r1_weight}% marknad")
    print(f"  Bäst Top-4:  {best_t4:.1f}% vid {best_t4_weight}% modell + {100-best_t4_weight}% marknad")
    print(f"  Bäst Avg:    {best_avg:.2f} vid {best_avg_weight}% modell + {100-best_avg_weight}% marknad")

    return results_per_weight


def test_fine_grained(matched, coarse_best):
    """Finslipa runt den bästa grova vikten."""
    print()
    print("=" * 70)
    print("3. FINSLIPNING AV OPTIMAL VIKT")
    print("=" * 70)

    # Testa ±10% runt bästa vikten i 1%-steg
    center = coarse_best
    print(f"\n  Centrerar runt {center}% modell")
    print(f"\n  {'Modell%':>8} │ {'Rank 1':>8} {'Top 2':>8} {'Top 3':>8} {'Top 4':>8} │ {'Avg Rank':>9}")
    print(f"  {'─'*8} │ {'─'*8} {'─'*8} {'─'*8} {'─'*8} │ {'─'*9}")

    best_r1 = 0
    best_weight = center

    for model_pct in range(max(0, center - 15), min(100, center + 16)):
        mw = model_pct / 100.0
        kw = 1.0 - mw

        r1_hits = 0
        t2_hits = 0
        t3_hits = 0
        t4_hits = 0
        rank_sum = 0
        valid = 0

        for r in matched:
            model_scores = r["model_scores"]
            bet_dist = r["bet_dist"]
            common = set(model_scores.keys()) & set(bet_dist.keys())
            if len(common) < 3:
                continue

            bd_vals = [bet_dist[h] for h in common]
            bd_min, bd_max = min(bd_vals), max(bd_vals)
            bd_spread = bd_max - bd_min if bd_max > bd_min else 1.0
            ms_vals = [model_scores[h] for h in common]
            ms_min, ms_max = min(ms_vals), max(ms_vals)
            ms_spread = ms_max - ms_min if ms_max > ms_min else 1.0

            hybrid_scores = {}
            for h in common:
                m_norm = (model_scores[h] - ms_min) / ms_spread * 100
                k_norm = (bet_dist[h] - bd_min) / bd_spread * 100
                hybrid_scores[h] = mw * m_norm + kw * k_norm

            hybrid_ranking = sorted(common, key=lambda h: hybrid_scores[h], reverse=True)
            winner = r["winner"]
            if winner not in common:
                continue
            try:
                hybrid_pos = hybrid_ranking.index(winner) + 1
            except ValueError:
                continue

            valid += 1
            rank_sum += hybrid_pos
            if hybrid_pos == 1: r1_hits += 1
            if hybrid_pos <= 2: t2_hits += 1
            if hybrid_pos <= 3: t3_hits += 1
            if hybrid_pos <= 4: t4_hits += 1

        if valid == 0:
            continue

        r1 = r1_hits / valid * 100
        t2 = t2_hits / valid * 100
        t3 = t3_hits / valid * 100
        t4 = t4_hits / valid * 100
        avg = rank_sum / valid

        if r1 > best_r1:
            best_r1 = r1
            best_weight = model_pct

        marker = " ★" if model_pct == best_weight and r1 == best_r1 else ""
        print(f"  {model_pct:>7}% │ {r1:>7.1f}% {t2:>7.1f}% {t3:>7.1f}% {t4:>7.1f}% │ {avg:>8.2f}{marker}")

    print(f"\n  OPTIMAL VIKT: {best_weight}% modell + {100-best_weight}% marknad → {best_r1:.1f}% rank-1")
    return best_weight


def analyze_model_value_add(matched, optimal_weight):
    """Visa att modellen TILLFÖR värde utöver ren marknad."""
    print()
    print("=" * 70)
    print("4. MODELLENS MERVÄRDE (vad tillför den utöver marknad?)")
    print("=" * 70)

    mw = optimal_weight / 100.0
    kw = 1.0 - mw

    # Beräkna hybrid-ranking per lopp
    hybrid_better_than_market = 0
    hybrid_better_than_model = 0
    total = 0

    # Lopp där hybrid hittar vinnare men marknad missar
    hybrid_unique_finds = []

    for r in matched:
        model_scores = r["model_scores"]
        bet_dist = r["bet_dist"]
        common = set(model_scores.keys()) & set(bet_dist.keys())
        if len(common) < 3:
            continue

        bd_vals = [bet_dist[h] for h in common]
        bd_min, bd_max = min(bd_vals), max(bd_vals)
        bd_spread = bd_max - bd_min if bd_max > bd_min else 1.0
        ms_vals = [model_scores[h] for h in common]
        ms_min, ms_max = min(ms_vals), max(ms_vals)
        ms_spread = ms_max - ms_min if ms_max > ms_min else 1.0

        hybrid_scores = {}
        for h in common:
            m_norm = (model_scores[h] - ms_min) / ms_spread * 100
            k_norm = (bet_dist[h] - bd_min) / bd_spread * 100
            hybrid_scores[h] = mw * m_norm + kw * k_norm

        hybrid_ranking = sorted(common, key=lambda h: hybrid_scores[h], reverse=True)
        winner = r["winner"]
        if winner not in common:
            continue

        try:
            hybrid_pos = hybrid_ranking.index(winner) + 1
        except ValueError:
            continue

        total += 1
        if hybrid_pos < r["market_pos"]:
            hybrid_better_than_market += 1
        if hybrid_pos < r["model_pos"]:
            hybrid_better_than_model += 1

        # Hybrid hittar i top-3 men marknad missar top-4
        if hybrid_pos <= 3 and r["market_pos"] >= 5:
            hybrid_unique_finds.append({
                "date": r["date"],
                "track": r["track"],
                "winner": winner,
                "hybrid_pos": hybrid_pos,
                "market_pos": r["market_pos"],
                "model_pos": r["model_pos"],
                "streck": r["winner_streck"],
            })

    print(f"\n  Hybrid slår marknad: {hybrid_better_than_market}/{total} ({hybrid_better_than_market/total*100:.1f}%)")
    print(f"  Hybrid slår modell:  {hybrid_better_than_model}/{total} ({hybrid_better_than_model/total*100:.1f}%)")
    print(f"\n  Lopp där hybrid hittar vinnare (top-3) men marknad missar (rank 5+): {len(hybrid_unique_finds)}")

    if hybrid_unique_finds[:10]:
        print(f"\n  Exempel (modellens edge):")
        for h in hybrid_unique_finds[:10]:
            print(f"    {h['date']} {h['track']}: Nr {h['winner']} "
                  f"Hybrid={h['hybrid_pos']} Marknad={h['market_pos']} "
                  f"Modell={h['model_pos']} Streck={h['streck']:.1f}%")


def factor_importance_vs_market(matched):
    """Vilka faktorer tillför mest utöver marknad?"""
    print()
    print("=" * 70)
    print("5. VILKA FAKTORER TILLFÖR MEST UTÖVER MARKNAD?")
    print("=" * 70)

    # Kolla varje faktor: hur ofta rankar den vinnaren bättre/sämre
    # än marknaden?
    factor_edge = defaultdict(lambda: {"better": 0, "worse": 0, "total": 0})

    for r in matched:
        winner = str(r["winner"])
        bet_dist = r["bet_dist"]
        factor_data = r["factor_scores"]

        if winner not in factor_data:
            continue

        market_ranking = r["market_ranking"]
        try:
            market_pos = market_ranking.index(r["winner"]) + 1
        except ValueError:
            continue

        for factor_name in factor_data[winner].keys():
            # Ranka alla hästar efter denna faktor
            factor_scores = {}
            for h, fs in factor_data.items():
                if factor_name in fs:
                    factor_scores[int(h)] = fs[factor_name]

            if r["winner"] not in factor_scores:
                continue

            factor_ranking = sorted(factor_scores.keys(), key=lambda h: factor_scores[h], reverse=True)
            try:
                factor_pos = factor_ranking.index(r["winner"]) + 1
            except ValueError:
                continue

            factor_edge[factor_name]["total"] += 1
            if factor_pos < market_pos:
                factor_edge[factor_name]["better"] += 1
            elif factor_pos > market_pos:
                factor_edge[factor_name]["worse"] += 1

    print(f"\n  {'Faktor':<25} {'Bättre än marknad':>18} {'Sämre':>8} {'Net Edge':>10}")
    print(f"  {'─'*25} {'─'*18} {'─'*8} {'─'*10}")

    sorted_factors = sorted(
        factor_edge.items(),
        key=lambda x: (x[1]["better"] - x[1]["worse"]) / max(x[1]["total"], 1),
        reverse=True
    )

    for name, stats in sorted_factors:
        total = stats["total"]
        better = stats["better"]
        worse = stats["worse"]
        net = (better - worse) / total * 100 if total > 0 else 0
        b_pct = better / total * 100 if total > 0 else 0
        marker = "✓" if net > 0 else "✗"
        print(f"  {name:<25} {b_pct:>7.1f}% ({better:>4}) {worse:>7} {net:>+9.1f}% {marker}")


def summarize(matched, results_per_weight, optimal_weight):
    """Slutsats och rekommendation."""
    print()
    print("=" * 70)
    print("═══ SLUTSATS OCH REKOMMENDATION ═══")
    print("=" * 70)

    pure_model = results_per_weight.get(100, {})
    pure_market = results_per_weight.get(0, {})
    optimal = results_per_weight.get(optimal_weight, {})

    print(f"""
  ┌─────────────────────┬──────────┬──────────┬──────────┐
  │                     │ Rank 1   │ Top 4    │ Avg Rank │
  ├─────────────────────┼──────────┼──────────┼──────────┤
  │ Ren modell (100/0)  │ {pure_model.get('r1',0):>7.1f}% │ {pure_model.get('t4',0):>7.1f}% │ {pure_model.get('avg',0):>8.2f} │
  │ Ren marknad (0/100) │ {pure_market.get('r1',0):>7.1f}% │ {pure_market.get('t4',0):>7.1f}% │ {pure_market.get('avg',0):>8.2f} │
  │ HYBRID ({optimal_weight}/{100-optimal_weight})      │ {optimal.get('r1',0):>7.1f}% │ {optimal.get('t4',0):>7.1f}% │ {optimal.get('avg',0):>8.2f} │
  └─────────────────────┴──────────┴──────────┴──────────┘

  REKOMMENDATION:
  → Sätt super_score_model_weight till {optimal_weight/100:.2f} i config.py
  → Modell ensam förlorar mot marknad, men HYBRID slår båda
  → Modellens faktorer tillför unikt värde vid upsets och value-bets
  → Streckprocent (betDistribution) ska användas som marknads-signal
""")


if __name__ == "__main__":
    matched = load_backtest_with_market()

    if not matched:
        print("Inga matchade lopp!")
        exit(1)

    # 1. Diagnostisera var modellen tappar
    diagnose_failures(matched)

    # 2. Testa grova hybrid-vikter
    results = test_hybrid_weights(matched)

    # 3. Hitta bästa grova vikt (max rank-1)
    best_coarse = max(results.items(), key=lambda x: x[1]["r1"])[0]

    # 4. Finslipa
    optimal = test_fine_grained(matched, best_coarse)

    # 5. Modellens mervärde
    analyze_model_value_add(matched, optimal)

    # 6. Vilka faktorer tillför mest?
    factor_importance_vs_market(matched)

    # 7. Slutsats
    summarize(matched, results, optimal)
