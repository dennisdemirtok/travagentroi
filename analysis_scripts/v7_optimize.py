#!/usr/bin/env python3
"""Quick weight optimizer for v7 model — hill climbing on top3 accuracy."""

import sys
import os
import json
import re
import random
import logging
import copy
from datetime import date as dt_date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import FactorWeights, AnalysisConfig, CACHE_DIR

logging.basicConfig(level=logging.WARNING)

random.seed(42)


def find_cached_rounds(max_rounds=60):
    """Find cached V75/V85 rounds."""
    rounds = []
    calendar_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})_[a-f0-9]+\.json$")

    for fname in sorted(os.listdir(CACHE_DIR)):
        m = calendar_pattern.match(fname)
        if not m:
            continue
        day = m.group(1)
        try:
            with open(CACHE_DIR / fname) as f:
                cal = json.load(f)
        except Exception:
            continue

        games = cal.get("games", {})
        if isinstance(games, dict):
            for gt_key in ["V75", "V85"]:
                for game in games.get(gt_key, []):
                    if isinstance(game, dict):
                        gid = game.get("id", "")
                        if gid:
                            rounds.append((day, gid))

    seen = set()
    unique = []
    for day, gid in rounds:
        if day not in seen:
            seen.add(day)
            unique.append((day, gid))

    if len(unique) > max_rounds:
        unique = random.sample(unique, max_rounds)
        unique.sort()
    return unique


async def evaluate_weights(weights: FactorWeights, rounds_data: list) -> dict:
    """Evaluate a set of weights on pre-loaded round data."""
    config = AnalysisConfig(weights=weights, recent_starts_count=5)
    analyzer = CompositeAnalyzer(config)

    total_races = 0
    top1 = 0
    top2 = 0
    top3 = 0
    spik_total = 0
    spik_hits = 0

    for game_round in rounds_data:
        # Deep copy to avoid mutating originals
        gr = copy.deepcopy(game_round)
        analyzer.analyze_round(gr)

        for race in gr.races:
            if not race.result_order or not race.active_entries:
                continue
            winner = race.result_order[0]
            total_races += 1

            sorted_entries = sorted(
                race.active_entries, key=lambda e: e.super_score, reverse=True
            )
            ranking = [e.post_position for e in sorted_entries]

            if winner == ranking[0]:
                top1 += 1
            if winner in ranking[:2]:
                top2 += 1
            if winner in ranking[:3]:
                top3 += 1

            spik = next((e for e in sorted_entries if e.recommendation == "spik"), None)
            if spik:
                spik_total += 1
                if spik.post_position == winner:
                    spik_hits += 1

    if total_races == 0:
        return {"top1": 0, "top2": 0, "top3": 0, "races": 0}

    return {
        "top1": top1 / total_races,
        "top2": top2 / total_races,
        "top3": top3 / total_races,
        "spik_rate": spik_hits / spik_total if spik_total > 0 else 0,
        "spik_count": spik_total,
        "races": total_races,
    }


def random_weights() -> FactorWeights:
    """Generate random weights."""
    w = FactorWeights()
    factor_names = list(w.as_dict().keys())

    # Random values
    vals = {n: random.uniform(0.01, 0.25) for n in factor_names}

    # Low-value factors get lower max
    for low in ["driver_trainer", "equipment", "layoff"]:
        vals[low] = random.uniform(0.005, 0.05)

    # Normalize
    total = sum(vals.values())
    for k in vals:
        vals[k] /= total

    for k, v in vals.items():
        setattr(w, k, round(v, 4))

    return w


def perturb_weights(w: FactorWeights, strength: float = 0.03) -> FactorWeights:
    """Make a small random change to weights."""
    new_w = FactorWeights()
    d = w.as_dict()

    # Pick 2-3 factors to perturb
    factors = list(d.keys())
    n_perturb = random.randint(2, 4)
    to_perturb = random.sample(factors, min(n_perturb, len(factors)))

    for k in factors:
        v = d[k]
        if k in to_perturb:
            delta = random.gauss(0, strength)
            v = max(0.005, v + delta)
        setattr(new_w, k, v)

    # Normalize
    nd = new_w.as_dict()
    total = sum(nd.values())
    for k in nd:
        setattr(new_w, k, round(nd[k] / total, 4))

    return new_w


async def main():
    client = ATGClient()
    rounds = find_cached_rounds(60)
    print(f"Laddar {len(rounds)} omgangar...")

    # Pre-load all round data
    rounds_data = []
    for day, gid in rounds:
        parts = gid.split("_")
        gt = parts[0]
        day_date = dt_date.fromisoformat(day)

        try:
            game_round = await client.fetch_full_round(gt, day_date)
        except Exception:
            continue

        if not game_round or not game_round.races:
            continue

        # Temporal filtering
        race_date = game_round.round_date
        if isinstance(race_date, str):
            race_date = dt_date.fromisoformat(race_date)

        for race in game_round.races:
            if isinstance(race.race_date, str):
                race.race_date = dt_date.fromisoformat(str(race.race_date))
            for entry in race.entries:
                entry.horse.past_starts = [
                    s for s in entry.horse.past_starts
                    if (s.start_date if isinstance(s.start_date, dt_date) else dt_date.fromisoformat(str(s.start_date))) < race_date
                ]
                entry.horse.recompute_career_from_starts()
                entry.bet_percentage = None

        rounds_data.append(game_round)

    print(f"Laddade {len(rounds_data)} omgangar med data")

    # Current weights baseline
    current = FactorWeights()
    result = await evaluate_weights(current, rounds_data)
    print(f"\nBaseline v7: top1={result['top1']:.1%} top2={result['top2']:.1%} top3={result['top3']:.1%} "
          f"spik={result['spik_rate']:.1%}({result['spik_count']})")

    best_weights = current
    best_score = result["top1"] * 0.4 + result["top3"] * 0.6  # Balanced optimization
    best_result = result

    # Phase 1: Random search (50 iterations)
    print("\n--- Phase 1: Random search ---")
    for i in range(50):
        w = random_weights()
        r = await evaluate_weights(w, rounds_data)
        combined = r["top1"] * 0.4 + r["top3"] * 0.6
        if combined > best_score:
            best_score = combined
            best_weights = w
            best_result = r
            print(f"  [{i}] NEW BEST: top1={r['top1']:.1%} top2={r['top2']:.1%} top3={r['top3']:.1%}")

    print(f"\nBest after random search: combined={best_score:.3f}")

    # Phase 2: Hill climbing (100 iterations)
    print("\n--- Phase 2: Hill climbing ---")
    no_improve = 0
    for i in range(100):
        w = perturb_weights(best_weights, strength=0.02)
        r = await evaluate_weights(w, rounds_data)
        combined = r["top1"] * 0.4 + r["top3"] * 0.6
        if combined > best_score:
            best_score = combined
            best_weights = w
            best_result = r
            no_improve = 0
            print(f"  [{i}] IMPROVED: top1={r['top1']:.1%} top2={r['top2']:.1%} top3={r['top3']:.1%}")
        else:
            no_improve += 1
            if no_improve >= 30:
                print(f"  Convergerat vid iteration {i}")
                break

    # Final results
    print(f"\n{'='*60}")
    print(f"OPTIMERADE VIKTER v7")
    print(f"{'='*60}")
    print(f"Top-1: {best_result['top1']:.1%}")
    print(f"Top-2: {best_result['top2']:.1%}")
    print(f"Top-3: {best_result['top3']:.1%}")
    print(f"Spik:  {best_result['spik_rate']:.1%} ({best_result['spik_count']}st)")
    print(f"Lopp:  {best_result['races']}")
    print()

    d = best_weights.as_dict()
    for k, v in sorted(d.items(), key=lambda x: -x[1]):
        print(f"    {k}: float = {v:.4f}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
