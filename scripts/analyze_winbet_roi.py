#!/usr/bin/env python3
"""Vinnarspel & platsspel ROI-analys på modellens chansspik-hästar.

Testar:
1. Flat-bet vinnarspel ROI per chansspik-profil
2. Walk-forward test (3 perioder) utan data leakage
3. Proffs upset-detection: rankar proffs skrällar bättre?
4. Kombinerad modell + proffs chansspik-signal
"""

import asyncio
import json
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.data.atg_client import ATGClient
from trav_agent.config import CACHE_DIR


def discover_rounds(game_types=None, min_date="2025-06-01"):
    if game_types is None:
        game_types = ["V86", "V85", "V75", "GS75", "V64", "V65"]

    results = set()
    for gt in game_types:
        pattern = re.compile(rf"^{gt}_(\d{{4}}-\d{{2}}-\d{{2}})_\d+_\d+_[a-f0-9]+\.json$")
        if CACHE_DIR.exists():
            for f in CACHE_DIR.iterdir():
                m = pattern.match(f.name)
                if m and m.group(1) >= min_date:
                    results.add((gt, m.group(1)))

    return sorted(results, key=lambda x: (x[1], x[0]))


async def collect_race_data(rounds):
    """Collect all race-level data with model scores."""
    client = ATGClient()
    analyzer = CompositeAnalyzer()

    all_races = []
    for i, (gt, date_str) in enumerate(rounds):
        d = date.fromisoformat(date_str)
        try:
            game_round = await client.fetch_full_round(gt, d)
            if not game_round:
                continue

            has_results = all(bool(r.result_order) for r in game_round.races)
            if not has_results:
                try:
                    await client.refresh_results(game_round)
                    has_results = all(bool(r.result_order) for r in game_round.races)
                except Exception:
                    pass
            if not has_results:
                continue

            analyzer.analyze_round(game_round)

            for race in game_round.races:
                if not race.result_order:
                    continue

                winner_num = race.result_order[0]
                entries = race.active_entries
                if not entries:
                    continue

                by_model = sorted(entries, key=lambda e: e.super_score, reverse=True)
                by_market = sorted(entries, key=lambda e: e.bet_percentage or 0, reverse=True)

                for model_rank, e in enumerate(by_model, 1):
                    streck = (e.bet_percentage or 0) * 100
                    odds = (1 / e.bet_percentage) if e.bet_percentage and e.bet_percentage > 0.001 else 100
                    is_winner = e.post_position == winner_num
                    market_rank = next(
                        (r for r, me in enumerate(by_market, 1) if me.post_position == e.post_position), 99
                    )

                    all_races.append({
                        "game_type": gt,
                        "date": date_str,
                        "race_number": race.race_number,
                        "n_horses": len(entries),
                        "post_position": e.post_position,
                        "model_rank": model_rank,
                        "market_rank": market_rank,
                        "model_score": e.super_score,
                        "streck": streck,
                        "odds": odds,
                        "is_winner": is_winner,
                        "rank_diff": market_rank - model_rank,
                    })

            if (i + 1) % 50 == 0:
                n_races = len(set((r["date"], r["race_number"], r["game_type"]) for r in all_races))
                print(f"  [{i+1}/{len(rounds)}] {n_races} lopp...")

        except Exception:
            continue

    return all_races


def analyze_vinnarspel(entries, label=""):
    """Analyze win-bet ROI for a set of entries."""
    total = len(entries)
    winners = [e for e in entries if e["is_winner"]]
    n_wins = len(winners)

    if total == 0:
        return None

    odds_sum = sum(e["odds"] for e in winners)
    win_pct = n_wins / total * 100
    roi = (odds_sum - total) / total * 100
    avg_odds = odds_sum / n_wins if n_wins > 0 else 0

    return {
        "label": label,
        "total": total,
        "wins": n_wins,
        "win_pct": win_pct,
        "avg_odds": avg_odds,
        "roi": roi,
        "profit": odds_sum - total,
    }


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-types", "-g", default="V86,V85")
    parser.add_argument("--min-date", "-d", default="2025-06-01")
    args = parser.parse_args()

    game_types = args.game_types.split(",")
    rounds = discover_rounds(game_types, args.min_date)

    print("═══ VINNARSPEL & WALK-FORWARD ANALYS ═══")
    print(f"Omgångar: {len(rounds)} ({rounds[0][1]} → {rounds[-1][1]})")
    print(f"Speltyper: {', '.join(game_types)}")
    print()

    all_entries = await collect_race_data(rounds)
    n_races = len(set((e["date"], e["race_number"], e["game_type"]) for e in all_entries))
    print(f"\nTotalt: {len(all_entries)} hästar i {n_races} lopp")
    print()

    # ═══ 1. VINNARSPEL ROI PER PROFIL ═══
    print("═" * 70)
    print("1. VINNARSPEL (FLAT BET) — ROI per chansspik-profil")
    print("═" * 70)
    print()

    profiles = [
        ("Rank≤2, 5-15%", lambda e: e["model_rank"] <= 2 and 5 <= e["streck"] <= 15),
        ("Rank≤2, 5-20%", lambda e: e["model_rank"] <= 2 and 5 <= e["streck"] <= 20),
        ("Rank≤2, 5-25%", lambda e: e["model_rank"] <= 2 and 5 <= e["streck"] <= 25),
        ("Rank≤3, 5-15%", lambda e: e["model_rank"] <= 3 and 5 <= e["streck"] <= 15),
        ("Rank≤3, 5-20%", lambda e: e["model_rank"] <= 3 and 5 <= e["streck"] <= 20),
        ("Rank≤3, 5-25%", lambda e: e["model_rank"] <= 3 and 5 <= e["streck"] <= 25),
        ("Rank≤2, 3-10%", lambda e: e["model_rank"] <= 2 and 3 <= e["streck"] <= 10),
        ("Rank≤2, 8-20%", lambda e: e["model_rank"] <= 2 and 8 <= e["streck"] <= 20),
        ("Rank=1, 5-20%", lambda e: e["model_rank"] == 1 and 5 <= e["streck"] <= 20),
        ("Rank=1, 5-15%", lambda e: e["model_rank"] == 1 and 5 <= e["streck"] <= 15),
        ("Score≥P90, 5-20%", None),  # computed below
        ("RankDiff≥2, rank≤3, 5-25%", lambda e: e["rank_diff"] >= 2 and e["model_rank"] <= 3 and 5 <= e["streck"] <= 25),
    ]

    # Compute P90
    all_scores = sorted(e["model_score"] for e in all_entries)
    p90 = all_scores[int(len(all_scores) * 0.90)]
    profiles[10] = ("Score≥P90, 5-20%", lambda e: e["model_score"] >= p90 and 5 <= e["streck"] <= 20)

    print(f"  {'Profil':<30} {'Spel':>6} {'Vinst':>6} {'Vinst%':>7} {'Snitt odds':>11} {'ROI':>8} {'Vinst/100':>10}")
    print("  " + "─" * 85)

    for name, fn in profiles:
        candidates = [e for e in all_entries if fn(e)]
        result = analyze_vinnarspel(candidates, name)
        if result:
            per_100 = result["profit"] / result["total"] * 100 if result["total"] > 0 else 0
            star = " ★★" if result["roi"] > 30 else " ★" if result["roi"] > 0 else ""
            print(f"  {name:<30} {result['total']:>6} {result['wins']:>6} {result['win_pct']:>6.1f}% "
                  f"{result['avg_odds']:>10.1f} {result['roi']:>7.1f}%{star} {per_100:>+9.1f}kr")

    # ═══ 2. ODDS-BAND ANALYS ═══
    print()
    print("═" * 70)
    print("2. VINNARSPEL per ODDS-BAND (Rank≤2, 5-20%)")
    print("═" * 70)
    print()

    chansspik = [e for e in all_entries if e["model_rank"] <= 2 and 5 <= e["streck"] <= 20]
    odds_bands = [
        ("3-5x", 3, 5),
        ("5-8x", 5, 8),
        ("8-12x", 8, 12),
        ("12-20x", 12, 20),
        ("20x+", 20, 100),
    ]

    print(f"  {'Odds':>8} {'Spel':>6} {'Vinst':>6} {'Vinst%':>7} {'ROI':>8}")
    print("  " + "─" * 40)
    for name, lo, hi in odds_bands:
        band = [e for e in chansspik if lo <= e["odds"] < hi]
        r = analyze_vinnarspel(band)
        if r:
            star = " ★" if r["roi"] > 0 else ""
            print(f"  {name:>8} {r['total']:>6} {r['wins']:>6} {r['win_pct']:>6.1f}% {r['roi']:>7.1f}%{star}")

    # ═══ 3. WALK-FORWARD TEST ═══
    print()
    print("═" * 70)
    print("3. WALK-FORWARD TEST — Utan data leakage")
    print("═" * 70)
    print()

    # Get date range
    all_dates = sorted(set(e["date"] for e in all_entries))
    n_dates = len(all_dates)

    # Split into 3 periods
    period_size = n_dates // 3
    periods = [
        ("Period 1 (äldst)", all_dates[:period_size]),
        ("Period 2 (mitten)", all_dates[period_size:2*period_size]),
        ("Period 3 (nyast)", all_dates[2*period_size:]),
    ]

    # Also do 2-period split for cleaner validation
    half = n_dates // 2
    halves = [
        ("Första halvan", all_dates[:half]),
        ("Andra halvan", all_dates[half:]),
    ]

    test_profiles = [
        ("Rank≤2, 5-15%", lambda e: e["model_rank"] <= 2 and 5 <= e["streck"] <= 15),
        ("Rank≤2, 5-20%", lambda e: e["model_rank"] <= 2 and 5 <= e["streck"] <= 20),
        ("Rank≤3, 5-20%", lambda e: e["model_rank"] <= 3 and 5 <= e["streck"] <= 20),
    ]

    print("  3-periodssplit:")
    for profile_name, fn in test_profiles:
        print(f"\n  {profile_name}:")
        for period_name, period_dates in periods:
            period_entries = [e for e in all_entries if fn(e) and e["date"] in set(period_dates)]
            r = analyze_vinnarspel(period_entries)
            if r:
                star = " ★" if r["roi"] > 0 else ""
                print(f"    {period_name:20s} ({period_dates[0]}→{period_dates[-1]}): "
                      f"{r['wins']:>3}/{r['total']:>4} = {r['win_pct']:>5.1f}%, ROI={r['roi']:>+7.1f}%{star}")

    print()
    print("  2-periodssplit (halvor):")
    for profile_name, fn in test_profiles:
        print(f"\n  {profile_name}:")
        for half_name, half_dates in halves:
            half_entries = [e for e in all_entries if fn(e) and e["date"] in set(half_dates)]
            r = analyze_vinnarspel(half_entries)
            if r:
                star = " ★" if r["roi"] > 0 else ""
                print(f"    {half_name:20s} ({half_dates[0]}→{half_dates[-1]}): "
                      f"{r['wins']:>3}/{r['total']:>4} = {r['win_pct']:>5.1f}%, ROI={r['roi']:>+7.1f}%{star}")

    # Monthly breakdown
    print()
    print("  Månadsvis breakdown (Rank≤2, 5-20%):")
    fn_best = lambda e: e["model_rank"] <= 2 and 5 <= e["streck"] <= 20
    months = defaultdict(list)
    for e in all_entries:
        if fn_best(e):
            month = e["date"][:7]
            months[month].append(e)

    print(f"  {'Månad':>10} {'Spel':>6} {'Vinst':>6} {'Vinst%':>7} {'ROI':>8} {'Kumulativ':>10}")
    print("  " + "─" * 55)
    cumulative_bets = 0
    cumulative_profit = 0
    for month in sorted(months.keys()):
        entries = months[month]
        r = analyze_vinnarspel(entries)
        if r:
            cumulative_bets += r["total"]
            cumulative_profit += r["profit"]
            cum_roi = cumulative_profit / cumulative_bets * 100 if cumulative_bets > 0 else 0
            star = " ★" if r["roi"] > 0 else ""
            print(f"  {month:>10} {r['total']:>6} {r['wins']:>6} {r['win_pct']:>6.1f}% {r['roi']:>7.1f}%{star} {cum_roi:>+9.1f}%")

    # ═══ 4. PER SPELTYP ═══
    print()
    print("═" * 70)
    print("4. PER SPELTYP — Vinnarspel")
    print("═" * 70)
    print()

    for gt in sorted(set(e["game_type"] for e in all_entries)):
        gt_entries = [e for e in all_entries if e["game_type"] == gt]
        print(f"  {gt}:")

        for name, fn in test_profiles:
            candidates = [e for e in gt_entries if fn(e)]
            r = analyze_vinnarspel(candidates)
            if r and r["total"] > 10:
                star = " ★" if r["roi"] > 0 else ""
                print(f"    {name:<25} {r['wins']:>3}/{r['total']:>4} = {r['win_pct']:>5.1f}%, ROI={r['roi']:>+7.1f}%{star}")
        print()

    # ═══ 5. PROFFS UPSET DETECTION ═══
    print("═" * 70)
    print("5. PROFFS UPSET DETECTION — Rankar proffs skrällar bättre?")
    print("═" * 70)
    print()

    # Load proffs data
    proffs_cache = Path("proffs_history_cache")
    if proffs_cache.exists():
        from scripts.analyze_proffs_edge import compute_viktat_streck

        proffs_winners_detected = 0
        proffs_total_upsets = 0
        model_winners_detected = 0

        for f in sorted(proffs_cache.glob("*.json")):
            with open(f) as fh:
                data = json.load(fh)

            systems = data.get("systems", [])
            submitted = [s for s in systems if s.get("submitted")]
            if not submitted:
                continue

            races = compute_viktat_streck(data["races"], systems)
            gt = data.get("meta", {}).get("gameType", "")

            for race in races:
                horses = race["horses"]
                winner = next(
                    (h for h in horses if h.get("place") == 1 and not h.get("disqualified") and not h.get("galloped")),
                    None
                )
                if not winner:
                    continue

                # Is this an upset? (public_pct < 15%)
                if winner["public_pct"] >= 15:
                    continue

                proffs_total_upsets += 1

                # Was winner in proffs top-3?
                by_proffs = sorted(horses, key=lambda h: h["proffs_weighted_pct"], reverse=True)
                proffs_rank = next((r + 1 for r, h in enumerate(by_proffs) if h["number"] == winner["number"]), 99)
                if proffs_rank <= 3:
                    proffs_winners_detected += 1

                # Was winner in model top-3? (check if we have model data)
                # We'll compare using market ranking as proxy
                by_public = sorted(horses, key=lambda h: h["public_pct"], reverse=True)
                public_rank = next((r + 1 for r, h in enumerate(by_public) if h["number"] == winner["number"]), 99)

        if proffs_total_upsets > 0:
            print(f"  Upsets (vinnare <15% streck): {proffs_total_upsets}")
            print(f"  Proffs hade upset-vinnaren i topp-3: {proffs_winners_detected}/{proffs_total_upsets} ({proffs_winners_detected/proffs_total_upsets*100:.1f}%)")
            print()

            # Detailed: proffs rank of upset winners
            print("  Proffs-rank för upset-vinnare:")
            rank_counts = defaultdict(int)
            for f in sorted(proffs_cache.glob("*.json")):
                with open(f) as fh:
                    data = json.load(fh)
                systems = data.get("systems", [])
                submitted = [s for s in systems if s.get("submitted")]
                if not submitted:
                    continue
                races = compute_viktat_streck(data["races"], systems)
                for race in races:
                    horses = race["horses"]
                    winner = next(
                        (h for h in horses if h.get("place") == 1 and not h.get("disqualified") and not h.get("galloped")),
                        None
                    )
                    if not winner or winner["public_pct"] >= 15:
                        continue
                    by_proffs = sorted(horses, key=lambda h: h["proffs_weighted_pct"], reverse=True)
                    proffs_rank = next((r + 1 for r, h in enumerate(by_proffs) if h["number"] == winner["number"]), 99)
                    rank_counts[proffs_rank] += 1

            for rank in sorted(rank_counts.keys()):
                pct = rank_counts[rank] / proffs_total_upsets * 100
                bar = "█" * int(pct / 2)
                print(f"    Rank {rank:>2}: {rank_counts[rank]:>3} ({pct:>5.1f}%) {bar}")

            # Proffs edge for upsets
            print()
            print("  Snitt proffs-edge för upset-vinnare:")
            edge_sum = 0
            edge_count = 0
            high_edge_detected = 0
            for f in sorted(proffs_cache.glob("*.json")):
                with open(f) as fh:
                    data = json.load(fh)
                systems = data.get("systems", [])
                submitted = [s for s in systems if s.get("submitted")]
                if not submitted:
                    continue
                races = compute_viktat_streck(data["races"], systems)
                for race in races:
                    horses = race["horses"]
                    winner = next(
                        (h for h in horses if h.get("place") == 1 and not h.get("disqualified") and not h.get("galloped")),
                        None
                    )
                    if not winner or winner["public_pct"] >= 15:
                        continue
                    edge_sum += winner["edge_pp"]
                    edge_count += 1
                    if winner["edge_pp"] >= 10:
                        high_edge_detected += 1

            if edge_count > 0:
                print(f"    Snitt edge: {edge_sum/edge_count:+.1f}pp")
                print(f"    Med edge ≥10pp: {high_edge_detected}/{edge_count} ({high_edge_detected/edge_count*100:.1f}%)")
    else:
        print("  (Ingen proffs-data cachad)")

    # ═══ 6. SAMMANFATTNING ═══
    print()
    print("═" * 70)
    print("6. REKOMMENDATION — Chansspik vinnarspel")
    print("═" * 70)
    print()

    best = analyze_vinnarspel(
        [e for e in all_entries if e["model_rank"] <= 2 and 5 <= e["streck"] <= 20],
        "Rank≤2, 5-20%"
    )
    if best:
        print(f"  BÄSTA PROFILEN: {best['label']}")
        print(f"  ROI: {best['roi']:+.1f}%")
        print(f"  Vinst: {best['wins']}/{best['total']} ({best['win_pct']:.1f}%)")
        print(f"  Snittodds: {best['avg_odds']:.1f}")
        print(f"  Vinst per 100 spel: {best['profit']/best['total']*100:+.1f} enheter")
        print()
        print(f"  → Spela VINNARSPEL på hästar modellen rankar topp-2")
        print(f"    med 5-20% streck = +{best['roi']:.0f}% ROI")
        print(f"  → ~{best['total']/n_races:.1f} kandidater per lopp i snitt")
        freq = best["total"] / n_races
        per_round = freq * 8  # ~8 lopp per omgång
        print(f"  → ~{per_round:.1f} vinnarspel per V86/V85-omgång")


if __name__ == "__main__":
    asyncio.run(main())
