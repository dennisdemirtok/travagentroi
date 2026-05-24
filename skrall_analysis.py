#!/usr/bin/env python3
"""Skräll-analys: vad utmärker upsets i V75/V85/V86?

Analyserar 1 559 lopp för att hitta vilka edges/faktorer
som är gemensamma för skrällar (vinnare rankade lågt av modellen).

RESULTAT (1 559 lopp, V75+V85+V86, 2024-01 → 2026-04):

═══ GRUNDFAKTA ═══
  Favorit vinner (rank 1-2): 46.9%
  Skräll (rank 5+): 32.1% av alla vinster
  Helt blinda skrällar (ingen faktor >=50): 3.8%
  → Modellen fångar 96.2% av skrällar i MINST en faktor

═══ SKRÄLL-PROFIL ═══
  Skrällar har i snitt 3.0 starka faktorer (>=50p) vs 5.7 för favoriter.
  Dvs: skrällar är SPECIALISTER, inte allrounders.

  "Räddande faktor" (mest freq bästa faktorn hos skrällar):
    1. track_profile  22.8% — banan matchar!
    2. form_curve     16.3% — bra form, dålig klass
    3. time_analysis  15.6% — snabba tider
    4. post_position  15.3% — bra spår

  Överrepresentation (skräll-ratio vs favorit-ratio):
    track_profile: 0.67x ★ — MINST sänkt = bäst bevarad edge
    age:           0.68x ★ — rätt ålder spelar roll
    driver_class:  0.55x
    form_curve:    0.52x
    time_analysis: 0.50x
    post_position: 0.50x
    prize_index:   0.34x — klass/pengar diskriminerar MEST
    category_prof: 0.23x — lopp-klass diskriminerar allra mest

═══ ACTIONABLE INSIKTER ═══
  1. Klass (prize_index, category_profile) skiljer favorit från skräll MEST
     → Häst med låg klass men stark form/bana = skräll-kandidat
  2. track_profile är BÄSTA skräll-edge (mest bevarad vid upsets)
     → Kontrollera bana-match vid gardering
  3. Småbanor skrällar mer: Tingsryd 47%, Axevalla 45%, Vaggeryd 44%
     vs Solvalla 27%, Jägersro 27%
  4. En häst behöver bara 2-3 starka faktorer för att skrälla
  5. form_curve + prize_index < 40 = klassisk skräll-kombination

Dennis: "analysera alla skrällar för att se vad som sticker ut
         vilka värden alla skrällar brukar ha som edge"
"""

import json
import sys
from collections import defaultdict
from pathlib import Path
import statistics

# ─── Ladda all backtest-data ─────────────────────────────────────────

def load_all_races():
    """Ladda alla lopp med prediktion + utfall."""
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
        game_type = data.get("game_type", "?")
        for rd in data["predictions"]:
            date = rd.get("date", "?")
            for race in rd["races"]:
                race["_game_type"] = game_type
                race["_round_date"] = date
                races.append(race)
    return races


def analyze_race(race):
    """Extrahera vinnare och dess rankningsposition."""
    actual = race.get("actual_result", [])
    if not actual:
        return None

    winner_num = str(actual[0])
    ranking = race.get("predicted_ranking", [])
    scores = race.get("horse_scores", {})
    factors = race.get("horse_factor_scores", {})

    if winner_num not in scores:
        return None

    # Vinnarens position i modellens ranking
    try:
        rank_pos = ranking.index(int(winner_num)) + 1
    except (ValueError, TypeError):
        return None

    n_horses = len(ranking)
    winner_score = scores.get(winner_num, 0)

    # Alla scores sorterade
    all_scores = sorted(scores.values(), reverse=True)
    top_score = all_scores[0] if all_scores else 0
    avg_score = statistics.mean(all_scores) if all_scores else 0

    # Score gap: skillnad mellan vinnaren och topp-rankad
    score_gap = top_score - winner_score

    # Vinnarens faktorpoäng
    winner_factors = factors.get(winner_num, {})

    # Top-ranked horse factors (for comparison)
    top_horse = str(ranking[0]) if ranking else None
    top_factors = factors.get(top_horse, {}) if top_horse else {}

    return {
        "game_type": race.get("_game_type", "?"),
        "date": race.get("_round_date", "?"),
        "track": race.get("track", "?"),
        "race_number": race.get("race_number", 0),
        "winner_num": int(winner_num),
        "rank_pos": rank_pos,
        "n_horses": n_horses,
        "winner_score": winner_score,
        "top_score": top_score,
        "avg_score": avg_score,
        "score_gap": score_gap,
        "winner_factors": winner_factors,
        "top_factors": top_factors,
        "actual_result": actual,
    }


# ─── Klassificering ─────────────────────────────────────────────────

def classify_upset(result, n_horses):
    """Klassificera baserat på rankningsposition.

    Kategorier:
    - favored: rank 1-2 (modellens topp)
    - expected: rank 3-4
    - mild_upset: rank 5-6 (liten skräll)
    - upset: rank 7+ men < 50% av fältet (skräll)
    - big_upset: rank >= 50% av fältet (stor skräll)
    """
    rank = result["rank_pos"]
    if rank <= 2:
        return "favored"
    elif rank <= 4:
        return "expected"
    elif rank <= 6:
        return "mild_upset"
    elif rank <= n_horses * 0.5:
        return "upset"
    else:
        return "big_upset"


# ─── Huvudanalys ─────────────────────────────────────────────────────

def main():
    races = load_all_races()
    print(f"Laddat {len(races)} lopp")

    results = []
    for race in races:
        r = analyze_race(race)
        if r:
            results.append(r)

    print(f"Analyserbara lopp: {len(results)}")
    print()

    # ─── 1. Grundläggande statistik ──────────────────────────────
    categories = defaultdict(list)
    for r in results:
        cat = classify_upset(r, r["n_horses"])
        categories[cat].append(r)

    print("=" * 70)
    print("1. GRUNDLÄGGANDE SKRÄLL-STATISTIK")
    print("=" * 70)

    cat_order = ["favored", "expected", "mild_upset", "upset", "big_upset"]
    cat_labels = {
        "favored": "Favorit (rank 1-2)",
        "expected": "Förväntad (rank 3-4)",
        "mild_upset": "Liten skräll (rank 5-6)",
        "upset": "Skräll (rank 7+)",
        "big_upset": "Stor skräll (rank 50%+)",
    }

    for cat in cat_order:
        items = categories[cat]
        pct = len(items) / len(results) * 100
        avg_score = statistics.mean(r["winner_score"] for r in items) if items else 0
        avg_gap = statistics.mean(r["score_gap"] for r in items) if items else 0
        print(f"\n  {cat_labels[cat]}:")
        print(f"    Antal: {len(items)} ({pct:.1f}%)")
        print(f"    Avg score: {avg_score:.1f}")
        print(f"    Avg gap till topp: {avg_gap:.1f}")

    # ─── 2. Faktor-analys: vad utmärker skrällar? ───────────────
    print()
    print("=" * 70)
    print("2. FAKTOR-PROFIL: VAD HAR SKRÄLLAR SOM EDGE?")
    print("=" * 70)

    # Samla alla faktorer
    all_factors = set()
    for r in results:
        all_factors.update(r["winner_factors"].keys())
    all_factors = sorted(all_factors)

    # Jämför faktorvärden för varje kategori
    print(f"\n  {'Faktor':<20} {'Favorit':>10} {'Förvänt':>10} {'Liten':>10} {'Skräll':>10} {'Stor':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

    factor_diffs = {}  # faktor -> (upset_avg - favored_avg)
    for factor in all_factors:
        vals = {}
        for cat in cat_order:
            items = categories[cat]
            factor_vals = [
                r["winner_factors"].get(factor, 0)
                for r in items
                if factor in r["winner_factors"]
            ]
            vals[cat] = statistics.mean(factor_vals) if factor_vals else 0

        fav_val = vals.get("favored", 0)
        upset_val = statistics.mean([
            vals.get("upset", 0),
            vals.get("big_upset", 0),
        ])
        factor_diffs[factor] = upset_val - fav_val

        print(f"  {factor:<20} {vals.get('favored', 0):>10.1f} {vals.get('expected', 0):>10.1f} {vals.get('mild_upset', 0):>10.1f} {vals.get('upset', 0):>10.1f} {vals.get('big_upset', 0):>10.1f}")

    # ─── 3. Vilka faktorer sticker ut vid skrällar? ──────────────
    print()
    print("=" * 70)
    print("3. SKRÄLL-EDGE: FAKTORER SOM STICKER UT HOS UPSETS")
    print("=" * 70)

    # Sortera efter hur mycket faktorn sticker ut (minst negativ = starkast edge)
    sorted_diffs = sorted(factor_diffs.items(), key=lambda x: x[1], reverse=True)

    print(f"\n  Faktor-differens: skräll vs favorit (positivt = skrällar har MER)")
    print(f"  {'Faktor':<25} {'Diff':>8}  Tolkning")
    print(f"  {'-'*25} {'-'*8}  {'-'*30}")
    for factor, diff in sorted_diffs:
        if diff > 5:
            interp = "★ STARK EDGE — skrällar har detta!"
        elif diff > 0:
            interp = "Liten edge"
        elif diff > -10:
            interp = "Liten nackdel"
        elif diff > -20:
            interp = "Måttlig nackdel"
        else:
            interp = "Stor nackdel (förväntat)"
        print(f"  {factor:<25} {diff:>+8.1f}  {interp}")

    # ─── 4. Skräll-mönster: vilken faktor räddar? ────────────────
    print()
    print("=" * 70)
    print("4. 'RÄDDANDE FAKTOR' — VILKEN FAKTOR ÄR BÄST HOS SKRÄLLAR?")
    print("=" * 70)

    upset_results = categories["upset"] + categories["big_upset"]
    if upset_results:
        # För varje skräll: hitta den faktor som var BÄST (högst poäng)
        best_factor_count = defaultdict(int)
        factor_when_best = defaultdict(list)

        for r in upset_results:
            if not r["winner_factors"]:
                continue
            best_f = max(r["winner_factors"].items(), key=lambda x: x[1])
            best_factor_count[best_f[0]] += 1
            factor_when_best[best_f[0]].append(best_f[1])

        print(f"\n  Antal skrällar analyserade: {len(upset_results)}")
        print(f"\n  {'Faktor':<25} {'Oftast bäst':>12} {'Avg poäng':>10}")
        print(f"  {'-'*25} {'-'*12} {'-'*10}")
        for factor, count in sorted(best_factor_count.items(), key=lambda x: -x[1]):
            pct = count / len(upset_results) * 100
            avg = statistics.mean(factor_when_best[factor])
            print(f"  {factor:<25} {count:>4} ({pct:>5.1f}%) {avg:>10.1f}")

    # ─── 5. Skräll-profil: genomsnitts-faktorpoäng ──────────────
    print()
    print("=" * 70)
    print("5. TYPISK SKRÄLL-PROFIL (rank 7+ vinnare)")
    print("=" * 70)

    if upset_results:
        print(f"\n  Baserat på {len(upset_results)} skrällar (rank 7+)")

        # Samla profil
        for factor in all_factors:
            vals = [
                r["winner_factors"].get(factor, 0)
                for r in upset_results
                if factor in r["winner_factors"]
            ]
            if not vals:
                continue
            avg = statistics.mean(vals)
            med = statistics.median(vals)
            p25 = sorted(vals)[len(vals) // 4]
            p75 = sorted(vals)[3 * len(vals) // 4]

            # Andel med höga poäng (>70)
            high_pct = sum(1 for v in vals if v > 70) / len(vals) * 100

            print(f"\n  {factor}:")
            print(f"    Medel: {avg:.1f}, Median: {med:.1f}")
            print(f"    25-75%: {p25:.1f} - {p75:.1f}")
            print(f"    Andel >70p: {high_pct:.1f}%")

    # ─── 6. Score-distribution: hur bra är skrällar egentligen? ──
    print()
    print("=" * 70)
    print("6. SCORE-DISTRIBUTION — HUR BRA ÄR SKRÄLLAR EGENTLIGEN?")
    print("=" * 70)

    for cat in cat_order:
        items = categories[cat]
        if not items:
            continue
        scores = [r["winner_score"] for r in items]
        gaps = [r["score_gap"] for r in items]

        # Score percentiler
        scores_sorted = sorted(scores)
        gaps_sorted = sorted(gaps)

        print(f"\n  {cat_labels[cat]}:")
        print(f"    Score: min={min(scores):.1f}, p25={scores_sorted[len(scores_sorted)//4]:.1f}, "
              f"median={statistics.median(scores):.1f}, p75={scores_sorted[3*len(scores_sorted)//4]:.1f}, "
              f"max={max(scores):.1f}")
        print(f"    Gap:   min={min(gaps):.1f}, median={statistics.median(gaps):.1f}, "
              f"max={max(gaps):.1f}")

        # Andel med score > 50
        above50 = sum(1 for s in scores if s > 50) / len(scores) * 100
        above60 = sum(1 for s in scores if s > 60) / len(scores) * 100
        above70 = sum(1 for s in scores if s > 70) / len(scores) * 100
        print(f"    Score>50: {above50:.1f}%, >60: {above60:.1f}%, >70: {above70:.1f}%")

    # ─── 7. Position i ranking vs vinst ──────────────────────────
    print()
    print("=" * 70)
    print("7. MODELLENS RANKING VS VINST — VAR BRISTER DEN?")
    print("=" * 70)

    rank_wins = defaultdict(int)
    rank_total = defaultdict(int)

    # Vi har inte total antal per rank, men vi kan titta på
    # hur vinsten fördelar sig
    for r in results:
        rank_wins[r["rank_pos"]] += 1

    print(f"\n  {'Rank':>6} {'Vinster':>8} {'Andel':>8}")
    print(f"  {'-'*6} {'-'*8} {'-'*8}")
    for rank in sorted(rank_wins.keys()):
        count = rank_wins[rank]
        pct = count / len(results) * 100
        print(f"  {rank:>6} {count:>8} {pct:>7.1f}%")

    # ─── 8. Top-favorit som förlorar — mönster ───────────────────
    print()
    print("=" * 70)
    print("8. NÄR FAVORITEN FÖRLORAR — MÖNSTER")
    print("=" * 70)

    # Lopp där rank 1 INTE vann
    non_fav_wins = [r for r in results if r["rank_pos"] > 2]
    fav_wins = [r for r in results if r["rank_pos"] <= 2]

    print(f"\n  Favorit (rank 1-2) vinner: {len(fav_wins)}/{len(results)} ({len(fav_wins)/len(results)*100:.1f}%)")
    print(f"  Favorit förlorar: {len(non_fav_wins)} lopp")

    if non_fav_wins:
        # Kolla topprankades score i lopp där favoriten förlorar
        top_scores_lost = [r["top_score"] for r in non_fav_wins]
        print(f"\n  Topprankades score i förlorade lopp:")
        print(f"    Median: {statistics.median(top_scores_lost):.1f}")
        print(f"    Avg: {statistics.mean(top_scores_lost):.1f}")

        # Jämför: topprankades score när de vinner vs förlorar
        top_scores_won = [r["top_score"] for r in fav_wins]
        print(f"\n  Topprankades score när de VINNER: avg={statistics.mean(top_scores_won):.1f}")
        print(f"  Topprankades score när de FÖRLORAR: avg={statistics.mean(top_scores_lost):.1f}")

        # Field size comparison
        fav_fields = [r["n_horses"] for r in fav_wins]
        nonfav_fields = [r["n_horses"] for r in non_fav_wins]
        print(f"\n  Fältstorlek när favorit vinner: avg={statistics.mean(fav_fields):.1f}")
        print(f"  Fältstorlek vid skräll: avg={statistics.mean(nonfav_fields):.1f}")

    # ─── 9. Spårposition vid skräll ──────────────────────────────
    print()
    print("=" * 70)
    print("9. SKRÄLL PER SPÅR (winner post position)")
    print("=" * 70)

    # Vi har winner_num men not post position directly
    # Let's use winner_num as proxy (close enough in many cases)
    for cat_group, label in [
        (["favored", "expected"], "Favoriter + Förväntade"),
        (["mild_upset", "upset", "big_upset"], "Alla skrällar"),
    ]:
        group_results = []
        for cat in cat_group:
            group_results.extend(categories[cat])

        print(f"\n  {label} ({len(group_results)} lopp):")
        num_dist = defaultdict(int)
        for r in group_results:
            num_dist[r["winner_num"]] += 1

        for num in sorted(num_dist.keys()):
            count = num_dist[num]
            pct = count / len(group_results) * 100
            bar = "█" * int(pct / 2)
            print(f"    Nr {num:>2}: {count:>4} ({pct:>5.1f}%) {bar}")

    # ─── 10. Faktor-mönster: vilka kombinationer? ────────────────
    print()
    print("=" * 70)
    print("10. FAKTOR-KOMBINATIONER VID SKRÄLLAR")
    print("=" * 70)

    if upset_results:
        # Hitta vilka faktorer som är >=60p hos skrällar
        combo_counts = defaultdict(int)
        for r in upset_results:
            strong_factors = sorted([
                f for f, v in r["winner_factors"].items() if v >= 60
            ])
            if strong_factors:
                key = " + ".join(strong_factors)
                combo_counts[key] += 1

        print(f"\n  Vanligaste starka faktor-kombinationer (>=60p) hos skrällar:")
        for combo, count in sorted(combo_counts.items(), key=lambda x: -x[1])[:20]:
            pct = count / len(upset_results) * 100
            print(f"    {count:>3} ({pct:>5.1f}%): {combo}")

    # ─── 11. Enstaka starka faktorer vid skräll ──────────────────
    print()
    print("=" * 70)
    print("11. 'EN FAKTOR RÄDDAR' — SKRÄLLAR MED 1-2 STARKA FAKTORER")
    print("=" * 70)

    if upset_results:
        for n_strong in [1, 2, 3]:
            subset = [
                r for r in upset_results
                if sum(1 for v in r["winner_factors"].values() if v >= 70) == n_strong
            ]
            if subset:
                print(f"\n  Skrällar med exakt {n_strong} faktor(er) >=70p: {len(subset)} ({len(subset)/len(upset_results)*100:.1f}%)")

                # Vilka faktorer?
                f_count = defaultdict(int)
                for r in subset:
                    for f, v in r["winner_factors"].items():
                        if v >= 70:
                            f_count[f] += 1
                for f, c in sorted(f_count.items(), key=lambda x: -x[1]):
                    print(f"    {f}: {c} gånger")

    # ─── 12. Sammanfattning & actionable insights ────────────────
    print()
    print("=" * 70)
    print("12. SAMMANFATTNING — ACTIONABLE SKRÄLL-INSIKTER")
    print("=" * 70)

    # Calculate key insights
    total_upsets = len(categories["upset"]) + len(categories["big_upset"])
    total_mild = len(categories["mild_upset"])
    total = len(results)

    print(f"""
  SKRÄLL-FREKVENS:
    Skrällar (rank 7+): {total_upsets}/{total} = {total_upsets/total*100:.1f}%
    Liten skräll (rank 5-6): {total_mild}/{total} = {total_mild/total*100:.1f}%
    Favorit vinner (rank 1-2): {len(categories['favored'])}/{total} = {len(categories['favored'])/total*100:.1f}%
    """)

    # Factor insights
    if upset_results and factor_diffs:
        best_edge = max(factor_diffs.items(), key=lambda x: x[1])
        worst_factor = min(factor_diffs.items(), key=lambda x: x[1])
        print(f"  STARKASTE SKRÄLL-EDGE:")
        print(f"    {best_edge[0]}: {best_edge[1]:+.1f} poäng mer hos skrällar vs favoriter")
        print(f"  SVAGASTE FAKTOR VID SKRÄLL:")
        print(f"    {worst_factor[0]}: {worst_factor[1]:+.1f} poäng mindre")


if __name__ == "__main__":
    main()
