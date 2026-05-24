#!/usr/bin/env python3
"""Modell-edge chansspik-analys — hitta lönsamma upsets via modellens edge.

Dennis vill hitta hästar som:
- Modellen rankar högt (bra form, bra tider, bra analys)
- Men som INTE är dunderfavoriter i marknaden (5-25% streck)
- Dvs hästar med positiv modell-edge

Denna analys använder all cachad ATG-data (2000+ omgångar) och testar:
1. Modell topp-rankad vs marknads-favorit — vem vinner oftare?
2. Modell edge per streck-intervall — var finns lönsamheten?
3. Chansspik-profil: 5-25% streck + hög modellscore — vinner de?
4. ROI per edge-bucket (modell vs marknad)
5. Optimal chansspik-strategi baserat på modell-edge
"""

import asyncio
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.data.atg_client import ATGClient
from trav_agent.config import CACHE_DIR


def discover_rounds(game_types: list[str] = None, limit: int = 0) -> list[tuple[str, str]]:
    """Find cached rounds, return (game_type, date) pairs."""
    if game_types is None:
        game_types = ["V86", "V85", "V75", "GS75", "V64", "V65"]

    results = set()
    for gt in game_types:
        pattern = re.compile(rf"^{gt}_(\d{{4}}-\d{{2}}-\d{{2}})_\d+_\d+_[a-f0-9]+\.json$")
        if CACHE_DIR.exists():
            for f in CACHE_DIR.iterdir():
                m = pattern.match(f.name)
                if m:
                    results.add((gt, m.group(1)))

    results = sorted(results, key=lambda x: (x[1], x[0]))
    if limit > 0:
        results = results[-limit:]
    return results


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-types", "-g", default="V86,V85,V75,GS75,V64,V65")
    parser.add_argument("--limit", "-n", type=int, default=0)
    parser.add_argument("--min-date", "-d", default="2025-06-01")
    args = parser.parse_args()

    game_types = args.game_types.split(",")
    rounds = discover_rounds(game_types, args.limit)

    # Filter by min date
    rounds = [(gt, d) for gt, d in rounds if d >= args.min_date]

    print(f"═══ MODELL-EDGE CHANSSPIK-ANALYS ═══")
    print(f"Omgångar: {len(rounds)} ({rounds[0][1]} → {rounds[-1][1]})")
    print(f"Speltyper: {', '.join(game_types)}")
    print()

    client = ATGClient()
    analyzer = CompositeAnalyzer()

    # Collect race-level data
    all_races = []
    processed = 0
    errors = 0

    for i, (gt, date_str) in enumerate(rounds):
        from datetime import date
        d = date.fromisoformat(date_str)

        try:
            game_round = await client.fetch_full_round(gt, d)
            if not game_round:
                continue

            # Check results
            has_results = all(bool(r.result_order) for r in game_round.races)
            if not has_results:
                try:
                    await client.refresh_results(game_round)
                    has_results = all(bool(r.result_order) for r in game_round.races)
                except Exception:
                    pass

            if not has_results:
                continue

            # Analyze with model
            analyzer.analyze_round(game_round)

            for race in game_round.races:
                if not race.result_order:
                    continue

                winner_num = race.result_order[0]
                entries = race.active_entries
                if not entries:
                    continue

                # Sort by model score
                by_model = sorted(entries, key=lambda e: e.super_score, reverse=True)
                # Sort by market (bet_percentage)
                by_market = sorted(entries, key=lambda e: e.bet_percentage or 0, reverse=True)

                # Find winner info
                winner_entry = None
                winner_model_rank = None
                winner_market_rank = None
                for rank, e in enumerate(by_model, 1):
                    if e.post_position == winner_num:
                        winner_model_rank = rank
                        winner_entry = e
                        break
                for rank, e in enumerate(by_market, 1):
                    if e.post_position == winner_num:
                        winner_market_rank = rank
                        break

                if winner_entry is None:
                    continue

                winner_streck = (winner_entry.bet_percentage or 0) * 100
                winner_score = winner_entry.super_score

                # Collect all entries' data
                entries_data = []
                for model_rank, e in enumerate(by_model, 1):
                    market_rank = next(
                        (r for r, me in enumerate(by_market, 1) if me.post_position == e.post_position),
                        None
                    )
                    streck = (e.bet_percentage or 0) * 100
                    odds = (1 / (e.bet_percentage or 0.01)) if e.bet_percentage and e.bet_percentage > 0 else 100
                    is_winner = e.post_position == winner_num

                    entries_data.append({
                        "post_position": e.post_position,
                        "model_rank": model_rank,
                        "market_rank": market_rank,
                        "model_score": e.super_score,
                        "streck": streck,
                        "odds": odds,
                        "is_winner": is_winner,
                        "rank_diff": (market_rank or 99) - model_rank,  # positive = model ranks higher
                    })

                all_races.append({
                    "game_type": gt,
                    "date": date_str,
                    "race_number": race.race_number,
                    "n_horses": len(entries),
                    "winner_model_rank": winner_model_rank,
                    "winner_market_rank": winner_market_rank,
                    "winner_streck": winner_streck,
                    "winner_score": winner_score,
                    "entries": entries_data,
                })

            processed += 1
            if (i + 1) % 50 == 0:
                print(f"  [{i+1}/{len(rounds)}] {processed} omgångar, {len(all_races)} lopp...")

        except Exception as e:
            errors += 1
            continue

    print(f"\nTotalt: {len(all_races)} lopp från {processed} omgångar (errors: {errors})")
    print()

    if len(all_races) < 50:
        print("För lite data!")
        return

    total = len(all_races)

    # ═══ ANALYS 1: Modell vs Marknad — vem rankar vinnaren bättre? ═══
    print("═" * 70)
    print("1. MODELL vs MARKNAD — Vem rankar vinnaren högst?")
    print("═" * 70)
    print()

    model_ranks = defaultdict(int)
    market_ranks = defaultdict(int)

    for r in all_races:
        model_ranks[r["winner_model_rank"]] += 1
        market_ranks[r["winner_market_rank"]] += 1

    print(f"{'Rank':>6} {'Modell':>12} {'Marknad':>12} {'Diff':>8}")
    print("─" * 45)
    for rank in range(1, 13):
        m = model_ranks.get(rank, 0)
        mk = market_ranks.get(rank, 0)
        mp = m / total * 100
        mkp = mk / total * 100
        diff = mp - mkp
        marker = " ★" if diff > 2 else " ↓" if diff < -2 else ""
        print(f"  {rank:>4} {m:>5} ({mp:>5.1f}%) {mk:>5} ({mkp:>5.1f}%) {diff:>+6.1f}%{marker}")

    print()
    for n in [1, 2, 3, 5]:
        m_cum = sum(model_ranks.get(r, 0) for r in range(1, n + 1))
        mk_cum = sum(market_ranks.get(r, 0) for r in range(1, n + 1))
        print(f"  Topp-{n}: Modell {m_cum/total*100:.1f}% vs Marknad {mk_cum/total*100:.1f}% ({(m_cum-mk_cum)/total*100:+.1f}%)")

    # ═══ ANALYS 2: Modell-edge per streck-intervall ═══
    print()
    print("═" * 70)
    print("2. VINST-FREKVENS per STRECK-INTERVALL — Modell topp-3 vs alla")
    print("═" * 70)
    print()

    streck_buckets = defaultdict(lambda: {
        "total": 0, "wins": 0, "model_top3_total": 0, "model_top3_wins": 0,
        "odds_sum": 0, "model_top3_odds_sum": 0,
    })

    for race in all_races:
        for e in race["entries"]:
            streck = e["streck"]
            if streck < 3:
                bucket = "0-3%"
            elif streck < 5:
                bucket = "3-5%"
            elif streck < 10:
                bucket = "5-10%"
            elif streck < 15:
                bucket = "10-15%"
            elif streck < 25:
                bucket = "15-25%"
            elif streck < 40:
                bucket = "25-40%"
            else:
                bucket = "40%+"

            streck_buckets[bucket]["total"] += 1
            in_model_top3 = e["model_rank"] <= 3

            if in_model_top3:
                streck_buckets[bucket]["model_top3_total"] += 1

            if e["is_winner"]:
                streck_buckets[bucket]["wins"] += 1
                streck_buckets[bucket]["odds_sum"] += e["odds"]
                if in_model_top3:
                    streck_buckets[bucket]["model_top3_wins"] += 1
                    streck_buckets[bucket]["model_top3_odds_sum"] += e["odds"]

    bucket_order = ["0-3%", "3-5%", "5-10%", "10-15%", "15-25%", "25-40%", "40%+"]
    print(f"{'Streck':>8} {'Alla vinst%':>12} {'Modell top3':>12} {'Lift':>7} {'ROI alla':>10} {'ROI top3':>10}")
    print("─" * 70)
    for bucket in bucket_order:
        d = streck_buckets.get(bucket)
        if not d or d["total"] == 0:
            continue

        all_pct = d["wins"] / d["total"] * 100
        t3_pct = d["model_top3_wins"] / d["model_top3_total"] * 100 if d["model_top3_total"] > 0 else 0
        lift = t3_pct / all_pct if all_pct > 0 else 0

        roi_all = (d["odds_sum"] - d["total"]) / d["total"] * 100 if d["total"] > 0 else 0
        roi_t3 = (d["model_top3_odds_sum"] - d["model_top3_total"]) / d["model_top3_total"] * 100 if d["model_top3_total"] > 0 else 0

        star = " ★" if lift > 1.5 and d["model_top3_total"] > 20 else ""
        print(f"  {bucket:>6} {all_pct:>7.1f}% ({d['wins']:>4}) {t3_pct:>7.1f}% ({d['model_top3_wins']:>4}) {lift:>6.2f}x {roi_all:>9.1f}% {roi_t3:>9.1f}%{star}")

    # ═══ ANALYS 3: Chansspik-profil — hög score + lågt streck ═══
    print()
    print("═" * 70)
    print("3. CHANSSPIK-PROFIL — Modellens bästa låg-streck hästar")
    print("═" * 70)
    print()

    # Test different model rank + streck thresholds
    print("  Modell rank 1-2 + streckintervall:")
    print(f"  {'Streck':>10} {'Antal':>8} {'Vinst':>8} {'Vinst%':>8} {'Snitt odds':>11} {'ROI':>8}")
    print("  " + "─" * 60)

    for max_streck_lo, max_streck_hi in [(3, 8), (5, 12), (5, 15), (5, 20), (5, 25), (8, 20), (10, 25)]:
        count = 0
        wins = 0
        odds_sum = 0
        for race in all_races:
            for e in race["entries"]:
                if e["model_rank"] <= 2 and max_streck_lo <= e["streck"] <= max_streck_hi:
                    count += 1
                    if e["is_winner"]:
                        wins += 1
                        odds_sum += e["odds"]

        if count > 0:
            pct = wins / count * 100
            avg_odds = odds_sum / wins if wins > 0 else 0
            roi = (odds_sum - count) / count * 100
            star = " ★" if roi > 0 else ""
            print(f"  {f'{max_streck_lo}-{max_streck_hi}%':>10} {count:>8} {wins:>8} {pct:>7.1f}% {avg_odds:>10.1f} {roi:>7.1f}%{star}")

    print()
    print("  Modell rank 1-3 + streckintervall:")
    print(f"  {'Streck':>10} {'Antal':>8} {'Vinst':>8} {'Vinst%':>8} {'Snitt odds':>11} {'ROI':>8}")
    print("  " + "─" * 60)

    for max_streck_lo, max_streck_hi in [(3, 8), (5, 12), (5, 15), (5, 20), (5, 25), (8, 20), (10, 25)]:
        count = 0
        wins = 0
        odds_sum = 0
        for race in all_races:
            for e in race["entries"]:
                if e["model_rank"] <= 3 and max_streck_lo <= e["streck"] <= max_streck_hi:
                    count += 1
                    if e["is_winner"]:
                        wins += 1
                        odds_sum += e["odds"]

        if count > 0:
            pct = wins / count * 100
            avg_odds = odds_sum / wins if wins > 0 else 0
            roi = (odds_sum - count) / count * 100
            star = " ★" if roi > 0 else ""
            print(f"  {f'{max_streck_lo}-{max_streck_hi}%':>10} {count:>8} {wins:>8} {pct:>7.1f}% {avg_odds:>10.1f} {roi:>7.1f}%{star}")

    # ═══ ANALYS 4: Rank-diff edge (modell ranks higher than market) ═══
    print()
    print("═" * 70)
    print("4. RANK-DIFF EDGE — Modell rankar högre än marknaden")
    print("═" * 70)
    print()

    # rank_diff = market_rank - model_rank
    # Positive means model thinks horse is better than market
    diff_buckets = defaultdict(lambda: {"total": 0, "wins": 0, "odds_sum": 0})

    for race in all_races:
        for e in race["entries"]:
            rd = e["rank_diff"]
            if rd >= 5:
                bucket = "+5+"
            elif rd >= 3:
                bucket = "+3-4"
            elif rd >= 1:
                bucket = "+1-2"
            elif rd == 0:
                bucket = "0"
            elif rd >= -2:
                bucket = "-1-2"
            elif rd >= -4:
                bucket = "-3-4"
            else:
                bucket = "-5-"

            diff_buckets[bucket]["total"] += 1
            if e["is_winner"]:
                diff_buckets[bucket]["wins"] += 1
                diff_buckets[bucket]["odds_sum"] += e["odds"]

    diff_order = ["+5+", "+3-4", "+1-2", "0", "-1-2", "-3-4", "-5-"]
    print(f"  {'Rank diff':>10} {'Antal':>8} {'Vinst%':>8} {'ROI':>8}")
    print("  " + "─" * 40)
    for bucket in diff_order:
        d = diff_buckets.get(bucket)
        if not d or d["total"] == 0:
            continue
        pct = d["wins"] / d["total"] * 100
        roi = (d["odds_sum"] - d["total"]) / d["total"] * 100
        star = " ★" if roi > 0 else ""
        print(f"  {bucket:>10} {d['total']:>8} {pct:>7.1f}% {roi:>7.1f}%{star}")

    # ═══ ANALYS 5: Optimal chansspik — kombinera rank + streck + score ═══
    print()
    print("═" * 70)
    print("5. OPTIMAL CHANSSPIK — Score + streck + rank-diff")
    print("═" * 70)
    print()

    # Score percentile analysis for chansspik candidates
    all_scores = [e["model_score"] for race in all_races for e in race["entries"]]
    all_scores.sort()

    p75 = all_scores[int(len(all_scores) * 0.75)]
    p85 = all_scores[int(len(all_scores) * 0.85)]
    p90 = all_scores[int(len(all_scores) * 0.90)]

    print(f"  Score percentiler: P75={p75:.1f}, P85={p85:.1f}, P90={p90:.1f}")
    print()

    # Test chansspik definitions
    definitions = [
        ("Rank≤2, 5-20% streck", lambda e: e["model_rank"] <= 2 and 5 <= e["streck"] <= 20),
        ("Rank≤3, 5-20% streck", lambda e: e["model_rank"] <= 3 and 5 <= e["streck"] <= 20),
        ("Rank≤2, 5-15% streck", lambda e: e["model_rank"] <= 2 and 5 <= e["streck"] <= 15),
        ("Rank≤3, 5-15% streck", lambda e: e["model_rank"] <= 3 and 5 <= e["streck"] <= 15),
        ("Rank≤2, 8-25% streck", lambda e: e["model_rank"] <= 2 and 8 <= e["streck"] <= 25),
        ("RankDiff≥3, 5-20% streck", lambda e: e["rank_diff"] >= 3 and 5 <= e["streck"] <= 20),
        ("RankDiff≥3, 5-15% streck", lambda e: e["rank_diff"] >= 3 and 5 <= e["streck"] <= 15),
        ("RankDiff≥5, 5-25% streck", lambda e: e["rank_diff"] >= 5 and 5 <= e["streck"] <= 25),
        (f"Score≥{p85:.0f}, 5-20% streck", lambda e: e["model_score"] >= p85 and 5 <= e["streck"] <= 20),
        (f"Score≥{p90:.0f}, 5-20% streck", lambda e: e["model_score"] >= p90 and 5 <= e["streck"] <= 20),
        (f"Score≥{p85:.0f}, 5-15% streck", lambda e: e["model_score"] >= p85 and 5 <= e["streck"] <= 15),
        ("Rank≤2, RankDiff≥2, 5-25%", lambda e: e["model_rank"] <= 2 and e["rank_diff"] >= 2 and 5 <= e["streck"] <= 25),
        ("Rank≤3, RankDiff≥2, 5-20%", lambda e: e["model_rank"] <= 3 and e["rank_diff"] >= 2 and 5 <= e["streck"] <= 20),
    ]

    print(f"  {'Definition':<35} {'Antal':>6} {'Vinst':>6} {'Vinst%':>7} {'Snitt odds':>11} {'ROI':>8} {'per lopp':>8}")
    print("  " + "─" * 85)

    for name, fn in definitions:
        count = 0
        wins = 0
        odds_sum = 0
        races_with_candidate = 0

        for race in all_races:
            has_candidate = False
            for e in race["entries"]:
                if fn(e):
                    count += 1
                    has_candidate = True
                    if e["is_winner"]:
                        wins += 1
                        odds_sum += e["odds"]
            if has_candidate:
                races_with_candidate += 1

        if count > 0:
            pct = wins / count * 100
            avg_odds = odds_sum / wins if wins > 0 else 0
            roi = (odds_sum - count) / count * 100
            per_race = count / total
            star = " ★★" if roi > 20 else " ★" if roi > 0 else ""
            print(f"  {name:<35} {count:>6} {wins:>6} {pct:>6.1f}% {avg_odds:>10.1f} {roi:>7.1f}%{star} {per_race:>7.2f}")

    # ═══ ANALYS 6: Per speltyp ═══
    print()
    print("═" * 70)
    print("6. PER SPELTYP — Modell-edge chansspik")
    print("═" * 70)
    print()

    by_gt = defaultdict(list)
    for r in all_races:
        by_gt[r["game_type"]].append(r)

    for gt in sorted(by_gt.keys()):
        races = by_gt[gt]
        n = len(races)

        # Model vs market top-1
        m1 = sum(1 for r in races if r["winner_model_rank"] == 1)
        mk1 = sum(1 for r in races if r["winner_market_rank"] == 1)
        m3 = sum(1 for r in races if r["winner_model_rank"] and r["winner_model_rank"] <= 3)
        mk3 = sum(1 for r in races if r["winner_market_rank"] and r["winner_market_rank"] <= 3)

        # Chansspik: rank≤3, 5-20% streck
        cs_total = 0
        cs_wins = 0
        cs_odds = 0
        for race in races:
            for e in race["entries"]:
                if e["model_rank"] <= 3 and 5 <= e["streck"] <= 20:
                    cs_total += 1
                    if e["is_winner"]:
                        cs_wins += 1
                        cs_odds += e["odds"]

        cs_pct = cs_wins / cs_total * 100 if cs_total > 0 else 0
        cs_roi = (cs_odds - cs_total) / cs_total * 100 if cs_total > 0 else 0

        print(f"  {gt} ({n} lopp):")
        print(f"    Modell #1: {m1/n*100:.0f}% | Marknad #1: {mk1/n*100:.0f}% | Diff: {(m1-mk1)/n*100:+.1f}%")
        print(f"    Modell top3: {m3/n*100:.0f}% | Marknad top3: {mk3/n*100:.0f}% | Diff: {(m3-mk3)/n*100:+.1f}%")
        print(f"    Chansspik (rank≤3, 5-20%): {cs_wins}/{cs_total} = {cs_pct:.1f}%, ROI={cs_roi:+.1f}%")
        print()

    # ═══ ANALYS 7: Vinnande chansspik-exempel ═══
    print("═" * 70)
    print("7. VINNANDE CHANSSPIK-HÄSTAR (Rank≤3, 5-20% streck)")
    print("═" * 70)
    print()

    winners = []
    for race in all_races:
        for e in race["entries"]:
            if e["model_rank"] <= 3 and 5 <= e["streck"] <= 20 and e["is_winner"]:
                winners.append({
                    "gt": race["game_type"],
                    "date": race["date"],
                    "race": race["race_number"],
                    "rank": e["model_rank"],
                    "streck": e["streck"],
                    "odds": e["odds"],
                    "rank_diff": e["rank_diff"],
                })

    # Sort by odds descending (biggest upsets first)
    winners.sort(key=lambda w: w["odds"], reverse=True)

    print(f"  Totalt {len(winners)} vinnare (sorterat efter odds, topp 30):")
    print(f"  {'Datum':>12} {'Typ':>4} {'Avd':>4} {'Rank':>5} {'Streck':>7} {'Odds':>8} {'RankDiff':>9}")
    print("  " + "─" * 55)
    for w in winners[:30]:
        print(f"  {w['date']:>12} {w['gt']:>4} {w['race']:>4} {w['rank']:>5} {w['streck']:>6.1f}% {w['odds']:>7.1f} {w['rank_diff']:>+8}")

    if len(winners) > 30:
        print(f"  ... och {len(winners) - 30} till")

    avg_odds = sum(w["odds"] for w in winners) / len(winners) if winners else 0
    print(f"\n  Snitt odds bland vinnare: {avg_odds:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
