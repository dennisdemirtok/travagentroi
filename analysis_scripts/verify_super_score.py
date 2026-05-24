"""Verifierar super_score-implementering med full backtest.

Kör alla 97 omgångar genom den uppdaterade CompositeAnalyzer
och jämför super_score-resultat mot förväntningarna.
"""

import asyncio
import json
import sys
import os
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import DEFAULT_CONFIG


async def main():
    client = ATGClient()
    analyzer = CompositeAnalyzer()

    # Ladda alla cachade omgångar
    cache_dir = Path("cache")
    game_dirs = sorted(cache_dir.glob("V75_*")) + sorted(cache_dir.glob("V85_*"))

    print(f"Hittade {len(game_dirs)} cachade omgångar")
    print(f"Super score model weight: {DEFAULT_CONFIG.super_score_model_weight}")

    total_races = 0
    super_top1 = 0
    super_top3 = 0
    model_top1 = 0
    model_top3 = 0
    market_top1 = 0
    market_top3 = 0

    total_rank_super = 0.0
    total_rank_model = 0.0

    rounds_processed = 0
    races_with_results = 0

    for game_dir in game_dirs:
        game_id = game_dir.name
        parts = game_id.split("_")
        if len(parts) < 4:
            continue

        game_type = parts[0]
        game_date = parts[1]

        try:
            game_round = await client.fetch_full_round(game_type, date.fromisoformat(game_date))
        except Exception:
            continue

        if not game_round or not game_round.races:
            continue

        # Analysera (fyller i super_score)
        analyzer.analyze_round(game_round)
        rounds_processed += 1

        for race in game_round.races:
            if not race.result_order:
                continue

            winner = race.result_order[0]
            actual_top3 = set(race.result_order[:3])
            races_with_results += 1
            total_races += 1

            # Super score ranking (vad modellen nu använder)
            super_sorted = sorted(
                race.active_entries,
                key=lambda e: e.super_score,
                reverse=True,
            )

            # Model-only ranking (composite_score utan marknad)
            model_sorted = sorted(
                race.active_entries,
                key=lambda e: e.composite_score,
                reverse=True,
            )

            # Market ranking (streckprocent)
            market_sorted = sorted(
                race.active_entries,
                key=lambda e: e.bet_percentage or 0,
                reverse=True,
            )

            # Super score accuracy
            if super_sorted and super_sorted[0].post_position == winner:
                super_top1 += 1
            super_top3_nums = {e.post_position for e in super_sorted[:3]}
            if winner in super_top3_nums:
                super_top3 += 1

            # Hitta winner rank i super
            for i, e in enumerate(super_sorted):
                if e.post_position == winner:
                    total_rank_super += (i + 1)
                    break

            # Model-only accuracy
            if model_sorted and model_sorted[0].post_position == winner:
                model_top1 += 1
            model_top3_nums = {e.post_position for e in model_sorted[:3]}
            if winner in model_top3_nums:
                model_top3 += 1

            for i, e in enumerate(model_sorted):
                if e.post_position == winner:
                    total_rank_model += (i + 1)
                    break

            # Market accuracy
            if market_sorted and market_sorted[0].post_position == winner:
                market_top1 += 1
            market_top3_nums = {e.post_position for e in market_sorted[:3]}
            if winner in market_top3_nums:
                market_top3 += 1

    print(f"\n{'='*60}")
    print(f"  RESULTAT: {rounds_processed} omgångar, {total_races} lopp")
    print(f"{'='*60}")
    print(f"\n{'Metod':<20} {'Top-1':>8} {'Top-3':>8} {'SnittRank':>10}")
    print(f"{'-'*50}")

    if total_races > 0:
        print(f"{'Super Score':<20} {super_top1/total_races:>7.1%} {super_top3/total_races:>7.1%} "
              f"{total_rank_super/total_races:>9.2f}")
        print(f"{'Ren Modell':<20} {model_top1/total_races:>7.1%} {model_top3/total_races:>7.1%} "
              f"{total_rank_model/total_races:>9.2f}")
        print(f"{'Marknad (streck)':<20} {market_top1/total_races:>7.1%} {market_top3/total_races:>7.1%}")

    print(f"\n{'='*60}")
    print(f"  FÖRBÄTTRING Super vs Modell:")
    if total_races > 0:
        print(f"    Top-1: {(super_top1-model_top1)/total_races:+.1%}")
        print(f"    Top-3: {(super_top3-model_top3)/total_races:+.1%}")
        print(f"    Rank:  {(total_rank_super-total_rank_model)/total_races:+.2f}")
    print(f"{'='*60}")

    # Spara resultat
    results = {
        "total_rounds": rounds_processed,
        "total_races": total_races,
        "super_score": {
            "top1": super_top1 / total_races if total_races else 0,
            "top3": super_top3 / total_races if total_races else 0,
            "avg_rank": total_rank_super / total_races if total_races else 0,
        },
        "model_only": {
            "top1": model_top1 / total_races if total_races else 0,
            "top3": model_top3 / total_races if total_races else 0,
            "avg_rank": total_rank_model / total_races if total_races else 0,
        },
        "market": {
            "top1": market_top1 / total_races if total_races else 0,
            "top3": market_top3 / total_races if total_races else 0,
        },
    }

    with open("super_score_verification.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nSparat till super_score_verification.json")


if __name__ == "__main__":
    asyncio.run(main())
