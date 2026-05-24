#!/usr/bin/env python3
"""Grid search optimizer for factor weights.

Tests different weight combinations on historical data to find
the configuration that maximizes model edge (rank-1 accuracy).

Usage:
    cd trav-agent
    python scripts/optimize_weights.py --from 2025-06-01 --to 2026-05-01 --game V85
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from trav_agent.config import FactorWeights, AnalysisConfig
from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


def generate_weight_grid() -> list[dict[str, float]]:
    """Generate weight combinations to test.

    Key factors (from v9 analysis):
    - post_position: strongest structural signal
    - age: market underprices age profiles
    - driver_class: driver win percentage
    - category_profile: category fit
    - driver_trainer: driver-trainer combo + Gocciadoro

    We grid search these 5 factors, keeping others at 0.
    """
    # Candidate values for each factor
    post_values = [0.20, 0.30, 0.35, 0.40, 0.50]
    age_values = [0.15, 0.25, 0.35, 0.45]
    driver_values = [0.05, 0.10, 0.15, 0.20, 0.25]
    category_values = [0.00, 0.05, 0.10, 0.15]
    dt_values = [0.00, 0.05, 0.10]

    combos = []
    for post, age, drv, cat, dt in itertools.product(
        post_values, age_values, driver_values, category_values, dt_values
    ):
        total = post + age + drv + cat + dt
        if abs(total - 1.0) > 0.001:
            continue
        combos.append({
            "post_position": post,
            "age": age,
            "driver_class": drv,
            "category_profile": cat,
            "driver_trainer": dt,
        })

    return combos


def generate_blend_grid() -> list[float]:
    """Generate model/market blend values to test."""
    return [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]


async def fetch_rounds(
    game_type: str,
    start_date: date,
    end_date: date,
) -> list:
    """Fetch all rounds in date range."""
    client = ATGClient()
    rounds = []

    d = start_date
    while d <= end_date:
        try:
            cal = await client.get_calendar(d)
            games = cal.get("games", {})
            for gt, game_list in games.items():
                if gt.upper() != game_type.upper():
                    continue
                for g in game_list:
                    gid = g.get("id", "")
                    if gid:
                        game_round = await client.fetch_full_round(gt.upper(), d)
                        if game_round and game_round.is_finished:
                            rounds.append(game_round)
                        await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"Error fetching {d}: {e}")
        d += timedelta(days=1)

    return rounds


def evaluate_weights(
    rounds: list,
    weights_dict: dict[str, float],
    model_blend: float = 0.25,
) -> dict:
    """Evaluate a weight configuration on all rounds.

    Returns accuracy metrics.
    """
    config = AnalysisConfig(
        weights=FactorWeights(**{k: weights_dict.get(k, 0.0) for k in FactorWeights.__dataclass_fields__}),
        super_score_model_weight=model_blend,
    )
    analyzer = CompositeAnalyzer(config=config)

    total_races = 0
    rank1_correct = 0
    top3_overlap_sum = 0

    for game_round in rounds:
        # Re-analyze with new weights
        analyzer.analyze_round(game_round)

        for race in game_round.races:
            if not race.result_order:
                continue
            total_races += 1

            winner_num = race.result_order[0]
            actual_top3 = set(race.result_order[:3])

            sorted_entries = sorted(
                race.active_entries,
                key=lambda e: e.super_score,
                reverse=True,
            )
            if not sorted_entries:
                continue

            # Rank-1 accuracy
            if sorted_entries[0].post_position == winner_num:
                rank1_correct += 1

            # Top-3 overlap
            our_top3 = {e.post_position for e in sorted_entries[:3]}
            overlap = len(our_top3 & actual_top3) / 3.0
            top3_overlap_sum += overlap

    if total_races == 0:
        return {"rank1_acc": 0, "top3_overlap": 0, "races": 0}

    return {
        "rank1_acc": rank1_correct / total_races,
        "top3_overlap": top3_overlap_sum / total_races,
        "races": total_races,
    }


async def main():
    parser = argparse.ArgumentParser(description="Grid search for optimal factor weights")
    parser.add_argument("--game", default="V85", help="Game type (V85, V86, etc)")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=10, help="Show top N results")
    parser.add_argument("--blend-only", action="store_true", help="Only optimize blend, keep current weights")
    args = parser.parse_args()

    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)

    print(f"\n{'='*70}")
    print(f"FAKTOR-VIKTOPTIMERING — Grid Search")
    print(f"{'='*70}")
    print(f"Speltyp: {args.game}")
    print(f"Period: {start} → {end}")
    print()

    # Fetch rounds
    print("Hämtar omgångar...")
    rounds = await fetch_rounds(args.game, start, end)
    print(f"Hittade {len(rounds)} avslutade omgångar")

    if not rounds:
        print("Inga omgångar hittade!")
        return

    # Baseline: current weights
    current_weights = FactorWeights()
    baseline = evaluate_weights(rounds, current_weights.as_dict(), model_blend=0.25)
    print(f"\nBaseline (nuvarande vikter):")
    print(f"  Rank-1: {baseline['rank1_acc']:.1%}")
    print(f"  Top-3 overlap: {baseline['top3_overlap']:.1%}")
    print(f"  Lopp: {baseline['races']}")

    results = []

    if args.blend_only:
        # Only test different blend values with current weights
        blends = generate_blend_grid()
        print(f"\nTestar {len(blends)} blend-värden...")

        for i, blend in enumerate(blends):
            metrics = evaluate_weights(rounds, current_weights.as_dict(), model_blend=blend)
            results.append({
                "weights": current_weights.as_dict(),
                "blend": blend,
                **metrics,
            })
            if (i + 1) % 5 == 0:
                print(f"  [{i+1}/{len(blends)}]")
    else:
        # Full grid search
        weight_grid = generate_weight_grid()
        blends = generate_blend_grid()
        total = len(weight_grid) * len(blends)

        print(f"\nTestar {len(weight_grid)} viktkombinationer × {len(blends)} blend = {total} konfigurationer...")

        t0 = time.time()
        for i, weights in enumerate(weight_grid):
            for blend in blends:
                metrics = evaluate_weights(rounds, weights, model_blend=blend)
                results.append({
                    "weights": weights,
                    "blend": blend,
                    **metrics,
                })

            if (i + 1) % 50 == 0:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed
                remaining = (len(weight_grid) - i - 1) / rate
                print(f"  [{i+1}/{len(weight_grid)}] ~{remaining:.0f}s kvar")

    # Sort by rank-1 accuracy
    results.sort(key=lambda x: x["rank1_acc"], reverse=True)

    print(f"\n{'='*70}")
    print(f"TOP {args.top} KONFIGURATIONER")
    print(f"{'='*70}")

    for i, r in enumerate(results[:args.top]):
        w = r["weights"]
        active = {k: v for k, v in w.items() if v > 0}
        weight_str = " | ".join(f"{k}={v:.2f}" for k, v in sorted(active.items(), key=lambda x: -x[1]))

        edge = r["rank1_acc"] - baseline["rank1_acc"]
        edge_str = f"+{edge:.1%}" if edge > 0 else f"{edge:.1%}"
        star = " ★" if edge > 0 else ""

        print(f"\n  #{i+1}: Rank-1={r['rank1_acc']:.1%} (edge={edge_str}{star})")
        print(f"      Top-3={r['top3_overlap']:.1%} | Blend={r['blend']:.0%}")
        print(f"      {weight_str}")

    # Best vs baseline
    best = results[0]
    best_edge = best["rank1_acc"] - baseline["rank1_acc"]

    print(f"\n{'='*70}")
    print(f"REKOMMENDATION")
    print(f"{'='*70}")

    if best_edge > 0.005:
        active = {k: v for k, v in best["weights"].items() if v > 0}
        print(f"\n  Förbättring möjlig: {best_edge:+.1%} rank-1 edge")
        print(f"\n  Optimala vikter:")
        for k, v in sorted(active.items(), key=lambda x: -x[1]):
            current = getattr(current_weights, k, 0)
            change = "→" if abs(v - current) > 0.01 else "="
            print(f"    {k}: {current:.2f} {change} {v:.2f}")
        print(f"    super_score_model_weight: 0.25 → {best['blend']:.2f}")

        print(f"\n  Uppdatera config.py FactorWeights med dessa värden.")
    else:
        print(f"\n  Nuvarande vikter är redan optimala (ingen förbättring funnen).")
        print(f"  Bästa edge: {best_edge:+.1%}")


if __name__ == "__main__":
    asyncio.run(main())
