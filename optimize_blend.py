"""Optimerar super_score blend-ratio (modell vs marknad).

Testar 0%, 10%, 20%, ..., 100% modellvikt för att hitta optimalt.
"""

import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import AnalysisConfig, FactorWeights


async def main():
    client = ATGClient()
    cache_dir = Path("cache")
    game_dirs = sorted(cache_dir.glob("V75_*")) + sorted(cache_dir.glob("V85_*"))

    # Ladda alla omgångar en gång
    rounds = []
    for game_dir in game_dirs:
        parts = game_dir.name.split("_")
        if len(parts) < 4:
            continue
        game_type, game_date = parts[0], parts[1]
        try:
            gr = await client.fetch_full_round(game_type, date.fromisoformat(game_date))
            if gr and gr.races:
                rounds.append(gr)
        except Exception:
            continue

    print(f"Laddade {len(rounds)} omgångar")

    # Testa olika blend-ratios
    print(f"\n{'Model%':>8} {'Top-1':>8} {'Top-3':>8} {'Rank':>8} {'V75 T3':>8} {'V85 T3':>8}")
    print("-" * 56)

    best_top3 = 0
    best_ratio = 0.5

    for model_pct in range(0, 105, 5):
        model_weight = model_pct / 100.0

        config = AnalysisConfig(
            super_score_model_weight=model_weight,
        )
        analyzer = CompositeAnalyzer(config=config)

        total = 0
        top1 = 0
        top3 = 0
        rank_sum = 0.0
        v75_total = 0
        v75_top3 = 0
        v85_total = 0
        v85_top3 = 0

        for gr in rounds:
            analyzer.analyze_round(gr)

            for race in gr.races:
                if not race.result_order:
                    continue
                winner = race.result_order[0]
                total += 1

                sorted_e = sorted(race.active_entries, key=lambda e: e.super_score, reverse=True)
                if sorted_e[0].post_position == winner:
                    top1 += 1
                if winner in {e.post_position for e in sorted_e[:3]}:
                    top3 += 1
                    if gr.game_type == "V75":
                        v75_top3 += 1
                    else:
                        v85_top3 += 1

                if gr.game_type == "V75":
                    v75_total += 1
                else:
                    v85_total += 1

                for i, e in enumerate(sorted_e):
                    if e.post_position == winner:
                        rank_sum += (i + 1)
                        break

        t1 = top1 / total if total else 0
        t3 = top3 / total if total else 0
        rk = rank_sum / total if total else 0
        v75t3 = v75_top3 / v75_total if v75_total else 0
        v85t3 = v85_top3 / v85_total if v85_total else 0

        flag = " ◀ BEST" if t3 > best_top3 else ""
        if t3 > best_top3:
            best_top3 = t3
            best_ratio = model_weight

        print(f"{model_pct:>7}% {t1:>7.1%} {t3:>7.1%} {rk:>7.2f} {v75t3:>7.1%} {v85t3:>7.1%}{flag}")

    print(f"\n{'='*56}")
    print(f"Bästa blend: {best_ratio*100:.0f}% modell / {(1-best_ratio)*100:.0f}% marknad → Top-3: {best_top3:.1%}")


if __name__ == "__main__":
    asyncio.run(main())
