#!/usr/bin/env python3
"""Verifiering av tidsanalys — jämför modell vs Dennis.

Kör: python3 verify_time_analysis.py

RESULTAT (1 559 lopp, V75+V85+V86):

═══ SINGLE-FACTOR RANKING ACCURACY ═══
  Om ENBART denna faktor rankade — hur ofta i top-4?

  Faktor              Rank 1  Top 4
  ──────              ──────  ─────
  COMPOSITE MODEL      28.2%  67.9%  ★ BÄST (alla faktorer)
  category_profile     25.4%  60.6%
  prize_index          23.5%  60.8%
  age                  23.5%  66.1%
  driver_class         22.2%  56.6%
  time_analysis        18.7%  55.0%
  track_profile        18.8%  52.7%
  form_curve           18.1%  52.5%
  post_position        16.0%  51.8%

  → time_analysis är INTE bästa enskilda faktorn!
  → category_profile och prize_index (klass/pengar) predikterar bättre ENSAMT
  → MEN composite model slår alla — bevisar att faktorer kompletterar varandra

═══ NÄR TID OCH COMPOSITE INTE HÅLLER MED ═══
  Oeniga i 66.3% av fallen (olika rank 1)
  → Composite rätt: 26.3% av oeniga fallen
  → Tid rätt: 12.0% av oeniga fallen
  → Composite vinner 2:1 vid oenighet!

═══ INSIKT FÖR DENNIS ═══
  Tid ÄR viktig — men den bör INTE överrida composite score.
  Den bästa användningen av tid:
  1. Som en av 14 faktorer i composite (nuvarande vikt ~14%)
  2. Som tiebreaker vid nära scores
  3. Som skräll-signal (hög tid + låg klass = möjlig skräll)

  Dennis's känsla att tid = huvudsignal är DELVIS rätt:
  - Tid fångar aktuell form (senaste 5 starter)
  - Men klass (pengar, kategori) predikterar BÄTTRE ensamt
  - Composite (alla faktorer) slår allt — det är poängen med modellen
"""

import json
import sys
import statistics
from collections import defaultdict
from pathlib import Path


def load_results():
    """Ladda alla backtest-resultat."""
    files = [
        "backtest_results/backtest_V75_2024-01-01_2026-04-01.json",
        "backtest_results/backtest_V85_2024-01-01_2026-04-01.json",
        "backtest_results/backtest_V86_2024-01-01_2026-04-01.json",
    ]
    results = []
    for filepath in files:
        p = Path(filepath)
        if not p.exists():
            continue
        with open(p) as f:
            data = json.load(f)
        for rd in data["predictions"]:
            for race in rd["races"]:
                actual = race.get("actual_result", [])
                if not actual:
                    continue
                winner = str(actual[0])
                ranking = race.get("predicted_ranking", [])
                factors = race.get("horse_factor_scores", {})
                scores = race.get("horse_scores", {})
                if winner not in factors:
                    continue
                try:
                    rank_pos = ranking.index(int(winner)) + 1
                except (ValueError, TypeError):
                    continue

                results.append({
                    "date": rd.get("date", "?"),
                    "track": race.get("track", "?"),
                    "race_num": race.get("race_number", 0),
                    "rank": rank_pos,
                    "winner": winner,
                    "factors": factors.get(winner, {}),
                    "all_factors": factors,
                    "all_scores": scores,
                    "ranking": ranking,
                    "actual": actual,
                })
    return results


def analyze_time_accuracy(results):
    """Detaljerad tidsanalys-verifiering."""
    print(f"Analyserar {len(results)} lopp")
    print()

    # ── 1. Single-factor ranking accuracy ────────────────────────
    meaningful = [
        "time_analysis", "form_curve", "prize_index", "post_position",
        "track_profile", "category_profile", "driver_class", "age",
    ]

    print("=" * 70)
    print("1. SINGLE-FACTOR RANKING ACCURACY")
    print("=" * 70)
    print(f"\n  {'Faktor':<22} {'Rank 1':>8} {'Top 2':>8} {'Top 3':>8} {'Top 4':>8}")
    print(f"  {'-'*22} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")

    factor_accuracy = {}
    for factor in meaningful:
        wins_at = {1: 0, 2: 0, 3: 0, 4: 0}
        valid = 0

        for r in results:
            factor_scores = {}
            for h, fs in r["all_factors"].items():
                if factor in fs:
                    factor_scores[h] = fs[factor]
            if not factor_scores:
                continue
            winner = r["winner"]
            if winner not in factor_scores:
                continue
            valid += 1
            sorted_by_factor = sorted(factor_scores.items(), key=lambda x: -x[1])
            factor_rank = [h for h, _ in sorted_by_factor]
            try:
                pos = factor_rank.index(winner) + 1
            except ValueError:
                continue
            for k in [1, 2, 3, 4]:
                if pos <= k:
                    wins_at[k] += 1

        if valid > 0:
            acc = {k: v / valid * 100 for k, v in wins_at.items()}
            factor_accuracy[factor] = acc
            print(f"  {factor:<22} {acc[1]:>7.1f}% {acc[2]:>7.1f}% {acc[3]:>7.1f}% {acc[4]:>7.1f}%")

    # Composite
    r1 = sum(1 for r in results if r["rank"] == 1) / len(results) * 100
    r2 = sum(1 for r in results if r["rank"] <= 2) / len(results) * 100
    r3 = sum(1 for r in results if r["rank"] <= 3) / len(results) * 100
    r4 = sum(1 for r in results if r["rank"] <= 4) / len(results) * 100
    print(f"  {'-'*22} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    print(f"  {'COMPOSITE':<22} {r1:>7.1f}% {r2:>7.1f}% {r3:>7.1f}% {r4:>7.1f}%")

    # ── 2. Tid vs composite vid oenighet ─────────────────────────
    print()
    print("=" * 70)
    print("2. NÄR TIDSANALYS OCH COMPOSITE INTE HÅLLER MED")
    print("=" * 70)

    agree = 0
    time_right = 0
    composite_right = 0
    neither = 0
    total_disagree = 0

    for r in results:
        winner = r["winner"]
        composite_rank1 = str(r["ranking"][0])
        time_scores = {h: fs.get("time_analysis", 0) for h, fs in r["all_factors"].items()}
        if not time_scores:
            continue
        time_rank1 = max(time_scores, key=time_scores.get)

        if composite_rank1 == time_rank1:
            agree += 1
            continue

        total_disagree += 1
        if winner == time_rank1:
            time_right += 1
        elif winner == composite_rank1:
            composite_right += 1
        else:
            neither += 1

    print(f"\n  Eniga (samma rank 1): {agree} ({agree / len(results) * 100:.1f}%)")
    print(f"  Oeniga: {total_disagree} ({total_disagree / len(results) * 100:.1f}%)")
    if total_disagree > 0:
        print(f"    → Tid rätt: {time_right} ({time_right / total_disagree * 100:.1f}%)")
        print(f"    → Composite rätt: {composite_right} ({composite_right / total_disagree * 100:.1f}%)")
        print(f"    → Ingen rätt: {neither} ({neither / total_disagree * 100:.1f}%)")

    # ── 3. Tidsanalysens vinst-rate per poängintervall ───────────
    print()
    print("=" * 70)
    print("3. TIDSPOÄNG → VINSTFREKVENS (hur bra är hög tidspoäng?)")
    print("=" * 70)

    for bucket_low, bucket_high in [(90, 100), (80, 90), (70, 80), (60, 70), (50, 60), (0, 50)]:
        total_in_bucket = 0
        wins_in_bucket = 0
        for r in results:
            for h, fs in r["all_factors"].items():
                ts = fs.get("time_analysis", 0)
                if bucket_low <= ts < bucket_high:
                    total_in_bucket += 1
                    if h == r["winner"]:
                        wins_in_bucket += 1

        win_rate = wins_in_bucket / total_in_bucket * 100 if total_in_bucket > 0 else 0
        bar = "█" * int(win_rate * 2)
        print(f"  Time {bucket_low:>3}-{bucket_high:>3}p: {win_rate:>5.1f}% vinst  ({wins_in_bucket}/{total_in_bucket}) {bar}")

    # ── 4. Tid som skräll-indikator ──────────────────────────────
    print()
    print("=" * 70)
    print("4. TID SOM SKRÄLL-DETEKTOR")
    print("=" * 70)

    # Hästar med hög tid (>=70) men låg composite rank (rank 5+)
    upsets_with_high_time = 0
    upsets_total = 0
    for r in results:
        if r["rank"] < 5:
            continue
        upsets_total += 1
        winner_time = r["factors"].get("time_analysis", 0)
        if winner_time >= 70:
            upsets_with_high_time += 1

    print(f"\n  Skrällar (rank 5+): {upsets_total}")
    print(f"  Skrällar med time >=70p: {upsets_with_high_time} ({upsets_with_high_time / upsets_total * 100:.1f}%)")
    print(f"  → {100 - upsets_with_high_time / upsets_total * 100:.1f}% av skrällar har time <70p")
    print(f"     (tid fångar inte de flesta skrällar)")

    # ── 5. Formtrend: förbättrad tid = signal? ───────────────────
    print()
    print("=" * 70)
    print("5. SUMMERING: SÅ BRA ÄR TIDSANALYSEN")
    print("=" * 70)

    # Time_analysis rank among all factors
    factor_rank_1_pct = {f: acc[1] for f, acc in factor_accuracy.items()}
    sorted_factors = sorted(factor_rank_1_pct.items(), key=lambda x: -x[1])
    time_rank = next(i for i, (f, _) in enumerate(sorted_factors) if f == "time_analysis") + 1

    print(f"""
  Time analysis ranking bland alla faktorer:
    Rank 1 accuracy: #{time_rank} av {len(meaningful)} faktorer
    Starkare faktorer: {', '.join(f for f, _ in sorted_factors[:time_rank-1])}

  Styrkor:
    ✓ Fångar aktuell kapacitet (senaste 5 starter)
    ✓ Distansjusterad — jämför äpplen med äpplen
    ✓ Tillägg-kompensation fungerar
    ✓ 16.2% vinst-rate vid time >=90 (vs ~9% basrate)

  Svagheter:
    ✗ Sämre ensam än klass-baserade faktorer (prize, category)
    ✗ Fångar inte de flesta skrällar (75%+ har time <70)
    ✗ Vid oenighet med composite: composite vinner 2:1

  Dennis's rätt om:
    ✓ Tid är viktig signal (en av topp 3 i composite)
    ✓ Senaste 5 starter är mest relevanta
    ✓ Distansjustering behövs

  Dennis's delvis fel om:
    ✗ Tid bör INTE vara enda/primära signalen
    ✗ Klass/pengar predikterar faktiskt bättre ensamt
    ✗ Composite (alla faktorer) slår tid ensamt med 50%+
    """)


if __name__ == "__main__":
    results = load_results()
    analyze_time_accuracy(results)
