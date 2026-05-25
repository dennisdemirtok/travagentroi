"""Vinnarspel ROI med KORREKT leakage prevention.

Testar: ger modellens vinnarspel-kandidater fortfarande
positiv ROI efter temporal filtrering + odds-neutralisering?
"""

import asyncio
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.data.atg_client import ATGClient
from trav_agent.backtest.runner import BacktestRunner
from trav_agent.config import CACHE_DIR


def discover_rounds(game_types=None, min_date="2025-06-01"):
    if game_types is None:
        game_types = ["V86", "V85", "V75", "GS75"]
    results = set()
    for gt in game_types:
        pattern = re.compile(rf"^{gt}_(\d{{4}}-\d{{2}}-\d{{2}})_\d+_\d+_[a-f0-9]+\.json$")
        if CACHE_DIR.exists():
            for f in CACHE_DIR.iterdir():
                m = pattern.match(f.name)
                if m and m.group(1) >= min_date:
                    results.add((gt, m.group(1)))
    return sorted(results, key=lambda x: (x[1], x[0]))


async def main():
    rounds_meta = discover_rounds(min_date="2025-06-01")
    print(f"Found {len(rounds_meta)} rounds from 2025-06-01+\n")

    client = ATGClient()
    analyzer = CompositeAnalyzer()

    all_candidates = []
    rounds_analyzed = 0

    for gt, date_str in rounds_meta:
        d = date.fromisoformat(date_str)
        try:
            gr = await client.fetch_full_round(gt, d)
            if not gr:
                continue
        except Exception:
            continue

        has_results = all(bool(r.result_order) for r in gr.races)
        if not has_results:
            continue

        # ═══ LEAKAGE PREVENTION ═══
        BacktestRunner._filter_future_starts(gr)
        BacktestRunner._neutralize_closing_odds(gr)

        analyzer.analyze_round(gr)
        rounds_analyzed += 1

        # Extract vinnarspel candidates
        for race in gr.races:
            if not race.result_order:
                continue

            winner_num = race.result_order[0]
            entries = race.active_entries
            if not entries:
                continue

            by_model = sorted(entries, key=lambda e: e.super_score, reverse=True)

            # P90 threshold for this round
            scores = [e.super_score for e in entries]
            scores_sorted = sorted(scores)
            p90_idx = int(len(scores_sorted) * 0.9)
            p90_threshold = scores_sorted[min(p90_idx, len(scores_sorted) - 1)]

            for model_rank, e in enumerate(by_model, 1):
                streck = e.bet_percentage or 0
                odds_est = (1 / streck) if streck > 0.01 else 50
                is_winner = e.post_position == winner_num
                driver_starts = getattr(e, "driver_starts_year", 0)
                driver_name = getattr(e, "driver_name", "") or ""

                all_candidates.append({
                    "date": date_str,
                    "gt": gt,
                    "race": race.race_number,
                    "name": e.horse.name,
                    "rank": model_rank,
                    "score": e.super_score,
                    "streck": streck,
                    "odds_est": odds_est,
                    "won": is_winner,
                    "driver": driver_name,
                    "driver_starts": driver_starts,
                    "is_p90": e.super_score >= p90_threshold,
                })

    print(f"Analyzed {rounds_analyzed} finished rounds")
    print(f"Total candidates: {len(all_candidates)}\n")

    # ═══ TEST PROFILES ═══
    BET = 500  # flat bet

    profiles = [
        ("Bas", lambda c: c["rank"] <= 2 and 0.05 <= c["streck"] <= 0.20),
        ("Pro (kusk≥100)", lambda c: c["rank"] <= 2 and 0.05 <= c["streck"] <= 0.20 and c["driver_starts"] >= 100),
        ("Sharp (score≥45+kusk)", lambda c: c["rank"] <= 2 and 0.05 <= c["streck"] <= 0.20 and c["driver_starts"] >= 100 and c["score"] >= 45),
        ("Sniper (kusk+3-10%)", lambda c: c["rank"] <= 2 and 0.03 <= c["streck"] <= 0.10 and c["driver_starts"] >= 100),
        ("Elite (P90)", lambda c: c["is_p90"] and 0.05 <= c["streck"] <= 0.20),
        ("Rank1 only", lambda c: c["rank"] == 1 and 0.05 <= c["streck"] <= 0.20),
        ("Rank1+kusk≥100", lambda c: c["rank"] == 1 and 0.05 <= c["streck"] <= 0.20 and c["driver_starts"] >= 100),
    ]

    print(f"{'PROFIL':<25} {'SPEL':>5} {'VINST':>6} {'VINST%':>7} {'ODDS':>7} {'INSATS':>10} {'UTBET':>10} {'NETTO':>10} {'ROI':>8}")
    print("=" * 95)

    for name, filt in profiles:
        filtered = [c for c in all_candidates if filt(c)]
        if not filtered:
            print(f"{name:<25} {'0':>5} -")
            continue

        wins = [c for c in filtered if c["won"]]
        n_bets = len(filtered)
        n_wins = len(wins)
        win_rate = n_wins / n_bets * 100 if n_bets > 0 else 0
        avg_odds = sum(c["odds_est"] for c in wins) / n_wins if n_wins > 0 else 0

        total_staked = n_bets * BET
        total_returned = sum(c["odds_est"] * BET for c in wins)
        netto = total_returned - total_staked
        roi = netto / total_staked * 100 if total_staked > 0 else 0

        marker = " ★" if roi > 0 else ""
        print(f"{name:<25} {n_bets:>5} {n_wins:>6} {win_rate:>6.1f}% {avg_odds:>6.1f}x "
              f"{total_staked:>9,} {total_returned:>9,.0f} {netto:>+9,.0f} {roi:>+7.1f}%{marker}")

    # ═══ PER MONTH ═══
    print(f"\n{'─'*80}")
    print("MÅNADSBRYTNING — Bas-profil (rank≤2, 5-20%):")
    bas = [c for c in all_candidates if c["rank"] <= 2 and 0.05 <= c["streck"] <= 0.20]
    by_month = defaultdict(lambda: {"bets": 0, "wins": 0, "staked": 0, "returned": 0})
    for c in bas:
        m = c["date"][:7]
        by_month[m]["bets"] += 1
        by_month[m]["staked"] += BET
        if c["won"]:
            by_month[m]["wins"] += 1
            by_month[m]["returned"] += c["odds_est"] * BET

    for month in sorted(by_month.keys()):
        d = by_month[month]
        roi = (d["returned"] - d["staked"]) / d["staked"] * 100 if d["staked"] > 0 else 0
        bar = "█" * max(0, int(roi / 10)) if roi > 0 else "▒" * min(5, max(0, int(-roi / 10)))
        print(f"  {month}: {d['bets']:>3} spel, {d['wins']:>2} vinster, "
              f"netto={d['returned']-d['staked']:>+8,.0f}, ROI={roi:>+6.1f}% {bar}")

    # ═══ SAVE ═══
    output = PROJECT_ROOT / "vinnarspel_leakage_test.json"
    save_data = {"rounds": rounds_analyzed, "candidates": len(all_candidates)}
    for name, filt in profiles:
        filtered = [c for c in all_candidates if filt(c)]
        wins = [c for c in filtered if c["won"]]
        n = len(filtered)
        w = len(wins)
        staked = n * BET
        returned = sum(c["odds_est"] * BET for c in wins)
        save_data[name] = {
            "bets": n, "wins": w,
            "win_rate": w / n * 100 if n > 0 else 0,
            "avg_odds": sum(c["odds_est"] for c in wins) / w if w > 0 else 0,
            "roi": (returned - staked) / staked * 100 if staked > 0 else 0,
            "netto": returned - staked,
        }
    with open(output, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"\nSparade resultat: {output}")


if __name__ == "__main__":
    asyncio.run(main())
