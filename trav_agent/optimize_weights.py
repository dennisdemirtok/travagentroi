"""Data-driven factor weight optimizer.

Loads ALL cached historical rounds, computes factor scores once,
then tests thousands of weight combinations to find what maximizes
top-1, top-2, and top-3 hit rates.

Key insight: factor scores are independent of weights — compute once,
re-combine cheaply.

Usage:
    python -m trav_agent.optimize_weights [--game-types V75,V85,V86,GS75]
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import numpy as np

from .analysis.composite import CompositeAnalyzer
from .backtest.runner import BacktestRunner
from .config import AnalysisConfig, FactorWeights
from .data.atg_client import ATGClient
from .data.models import GameRound, Race, RaceEntry

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

FACTOR_NAMES = [
    "time_analysis",
    "form_curve",
    "prize_index",
    "category_profile",
    "post_position",
    "driver_trainer",
    "track_profile",
]


@dataclass
class RaceData:
    """Pre-computed factor scores + actual result for one race."""

    race_number: int
    race_date: date
    track: str
    num_starters: int

    # Per horse: post_position → {factor_name: normalized_score}
    factor_scores: dict[int, dict[str, float]] = field(default_factory=dict)

    # Per horse: post_position → bet_percentage (0-1)
    bet_pcts: dict[int, float] = field(default_factory=dict)

    # Actual result: [winner_post, 2nd_post, 3rd_post, ...]
    result_order: list[int] = field(default_factory=list)

    @property
    def winner(self) -> Optional[int]:
        return self.result_order[0] if self.result_order else None


def _field_normalize(raw_scores: dict[int, float]) -> dict[int, float]:
    """Min-max normalize scores within a field to 0-100."""
    if not raw_scores:
        return {}
    vals = list(raw_scores.values())
    min_s, max_s = min(vals), max(vals)
    spread = max_s - min_s
    if spread < 5.0:
        return dict(raw_scores)  # Keep raw if spread too small
    return {
        pos: (s - min_s) / spread * 100.0
        for pos, s in raw_scores.items()
    }


def compute_composite(
    factor_scores: dict[str, float],
    weights: dict[str, float],
) -> float:
    """Weighted sum of factor scores → composite score."""
    total_w = sum(weights.values())
    if total_w == 0:
        return 50.0
    return sum(
        factor_scores.get(name, 50.0) * (w / total_w)
        for name, w in weights.items()
    )


def sigmoid_spread(raw: float, k: float = 0.08) -> float:
    """Sigmoid spreading to stretch middle scores."""
    return 100.0 / (1.0 + math.exp(-k * (raw - 50.0)))


def rank_horses(
    race: RaceData,
    weights: dict[str, float],
    market_weight: float = 0.15,
    use_sigmoid: bool = True,
) -> list[int]:
    """Rank horses by weighted composite + market blend. Returns post_positions in order."""
    composites = {}
    for pos, fs in race.factor_scores.items():
        c = compute_composite(fs, weights)
        if use_sigmoid:
            c = sigmoid_spread(c)
        composites[pos] = c

    # Market normalization
    if market_weight > 0 and race.bet_pcts:
        pcts = {pos: (race.bet_pcts.get(pos, 0.0)) * 100.0 for pos in composites}
        min_p, max_p = min(pcts.values()), max(pcts.values())
        spread = max_p - min_p
        if spread >= 1.0:
            market_scores = {
                pos: 5.0 + 90.0 * (p - min_p) / spread
                for pos, p in pcts.items()
            }
            model_w = 1.0 - market_weight
            for pos in composites:
                composites[pos] = (
                    model_w * composites[pos]
                    + market_weight * market_scores.get(pos, 50.0)
                )

    # Sort by score descending
    ranking = sorted(composites.keys(), key=lambda p: composites[p], reverse=True)
    return ranking


@dataclass
class OptResult:
    """Result of testing one weight configuration."""

    weights: dict[str, float]
    market_weight: float
    top1_rate: float  # Winner is our #1
    top2_rate: float  # Winner is in our top 2
    top3_rate: float  # Winner is in our top 3
    num_races: int

    # System-relevant: how often do ALL legs hit with N picks
    # (approximation using per-race hit rates)
    avg_picks_needed: float  # Average rank of winner

    @property
    def score(self) -> float:
        """Combined optimization target.

        For system betting, top2 and top3 matter most — we need to
        cover the winner with 2-3 picks per race, not necessarily
        pick the exact winner.
        """
        return self.top1_rate * 0.2 + self.top2_rate * 0.4 + self.top3_rate * 0.4


def evaluate_weights(
    races: list[RaceData],
    weights: dict[str, float],
    market_weight: float = 0.15,
    use_sigmoid: bool = True,
) -> OptResult:
    """Evaluate one weight configuration across all races."""
    top1 = 0
    top2 = 0
    top3 = 0
    total_winner_rank = 0
    n = 0

    for race in races:
        if not race.winner or len(race.factor_scores) < 3:
            continue

        ranking = rank_horses(race, weights, market_weight, use_sigmoid)
        winner = race.winner

        if winner not in ranking:
            continue

        n += 1
        winner_rank = ranking.index(winner) + 1
        total_winner_rank += winner_rank

        if winner_rank <= 1:
            top1 += 1
        if winner_rank <= 2:
            top2 += 1
        if winner_rank <= 3:
            top3 += 1

    if n == 0:
        return OptResult(weights, market_weight, 0, 0, 0, 0, 99)

    return OptResult(
        weights=weights,
        market_weight=market_weight,
        top1_rate=top1 / n,
        top2_rate=top2 / n,
        top3_rate=top3 / n,
        num_races=n,
        avg_picks_needed=total_winner_rank / n,
    )


async def load_all_races(
    game_types: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[RaceData]:
    """Load all cached historical rounds and extract pre-computed factor scores."""

    if game_types is None:
        game_types = ["V75", "V85", "V86", "GS75"]
    if start_date is None:
        start_date = date(2015, 1, 1)
    if end_date is None:
        end_date = date(2026, 4, 1)

    client = ATGClient()
    analyzer = CompositeAnalyzer()
    all_races: list[RaceData] = []

    for gt in game_types:
        print(f"Loading {gt}...", flush=True)
        count = 0

        async for day, game_round in client.fetch_historical_rounds_iter(
            gt, start_date, end_date
        ):
            if not game_round or not game_round.is_finished:
                continue

            # Filter future starts and recompute career (temporal integrity)
            BacktestRunner._filter_future_starts(game_round)
            # Neutralize closing odds — optimizer must not use market data
            BacktestRunner._neutralize_closing_odds(game_round)

            for race in game_round.races:
                if not race.result_order or len(race.active_entries) < 3:
                    continue

                # Score all factors (raw)
                for entry in race.active_entries:
                    fs = {}
                    for factor in analyzer.factors:
                        try:
                            fs[factor.name] = factor.score(entry, race)
                        except Exception:
                            fs[factor.name] = 40.0
                    entry.factor_scores = fs

                # Field-normalize each factor
                for factor_name in FACTOR_NAMES:
                    raw = {
                        e.post_position: e.factor_scores.get(factor_name, 40.0)
                        for e in race.active_entries
                    }
                    normalized = _field_normalize(raw)
                    for e in race.active_entries:
                        e.factor_scores[factor_name] = normalized.get(
                            e.post_position, 50.0
                        )

                # Store pre-computed data
                rd = RaceData(
                    race_number=race.race_number,
                    race_date=race.race_date,
                    track=race.track_name,
                    num_starters=len(race.active_entries),
                    factor_scores={
                        e.post_position: dict(e.factor_scores)
                        for e in race.active_entries
                    },
                    bet_pcts={
                        e.post_position: e.bet_percentage or 0.0
                        for e in race.active_entries
                    },
                    result_order=race.result_order,
                )
                all_races.append(rd)

            count += 1

        print(f"  {gt}: {count} rounds loaded", flush=True)

    print(f"\nTotal: {len(all_races)} races with results", flush=True)
    return all_races


def grid_search(
    races: list[RaceData],
    step: float = 0.05,
    market_weights: list[float] | None = None,
) -> list[OptResult]:
    """Grid search over weight space.

    Uses a coarse grid (step=0.05) with 7 factors.
    To keep it tractable, we fix low-impact factors (driver, track)
    and search the top 5 factors more aggressively.
    """
    if market_weights is None:
        market_weights = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30]

    results: list[OptResult] = []
    best_score = 0.0

    # Phase 1: Search main factors (time, form, prize, category, post)
    # Fix driver=0.05, track=0.05 initially
    print("\n=== Phase 1: Main factor search ===", flush=True)

    # Generate weight combinations that sum to ~0.90 (leaving 0.10 for driver+track)
    main_factors = ["time_analysis", "form_curve", "prize_index", "category_profile", "post_position"]
    combos_tested = 0

    # Use wider steps for 5-dimensional search
    weight_options = np.arange(0.05, 0.45, step)

    for mw in market_weights:
        for t in weight_options:
            for f in weight_options:
                for p in weight_options:
                    remainder = 0.90 - t - f - p
                    if remainder < 0.10:
                        continue
                    # Split remainder between category and post
                    for cat in np.arange(0.05, min(remainder, 0.35), step):
                        post = remainder - cat
                        if post < 0.03 or post > 0.25:
                            continue

                        weights = {
                            "time_analysis": round(float(t), 3),
                            "form_curve": round(float(f), 3),
                            "prize_index": round(float(p), 3),
                            "category_profile": round(float(cat), 3),
                            "post_position": round(float(post), 3),
                            "driver_trainer": 0.05,
                            "track_profile": 0.05,
                        }

                        result = evaluate_weights(races, weights, mw)
                        results.append(result)
                        combos_tested += 1

                        if result.score > best_score:
                            best_score = result.score
                            print(
                                f"  New best (#{combos_tested}): "
                                f"top1={result.top1_rate:.1%} "
                                f"top2={result.top2_rate:.1%} "
                                f"top3={result.top3_rate:.1%} "
                                f"mw={mw:.2f} "
                                f"w={weights}",
                                flush=True,
                            )

    print(f"  Phase 1: tested {combos_tested} combinations", flush=True)

    # Phase 2: Refine top-5 configs with fine-grained perturbations
    print("\n=== Phase 2: Refinement ===", flush=True)
    top5 = sorted(results, key=lambda r: r.score, reverse=True)[:5]

    refined_results: list[OptResult] = []
    fine_step = 0.02

    for base in top5:
        bw = base.weights
        # Try nearby market weights
        for mw in [base.market_weight - 0.05, base.market_weight, base.market_weight + 0.05]:
            if mw < 0 or mw > 0.50:
                continue
            # Try shifting weight between each pair of factors
            for i, f1 in enumerate(FACTOR_NAMES):
                for delta in [-fine_step, fine_step]:
                    w2 = dict(bw)
                    w2[f1] = max(0.02, w2[f1] + delta)
                    # Compensate by adjusting largest other factor
                    others = [f for f in FACTOR_NAMES if f != f1]
                    biggest = max(others, key=lambda f: w2[f])
                    w2[biggest] = max(0.02, w2[biggest] - delta)
                    # Renormalize
                    total = sum(w2.values())
                    w2 = {k: round(v / total, 3) for k, v in w2.items()}

                    result = evaluate_weights(races, w2, mw)
                    refined_results.append(result)

                    if result.score > best_score:
                        best_score = result.score
                        print(
                            f"  Refined: "
                            f"top1={result.top1_rate:.1%} "
                            f"top2={result.top2_rate:.1%} "
                            f"top3={result.top3_rate:.1%} "
                            f"mw={mw:.2f} "
                            f"w={w2}",
                            flush=True,
                        )

    results.extend(refined_results)
    print(f"  Phase 2: tested {len(refined_results)} additional combinations", flush=True)

    return results


def print_results(results: list[OptResult], top_n: int = 20) -> None:
    """Print top results."""
    sorted_results = sorted(results, key=lambda r: r.score, reverse=True)

    print("\n" + "=" * 100)
    print("TOP WEIGHT CONFIGURATIONS")
    print("=" * 100)

    # Current weights for comparison
    current = FactorWeights()
    current_w = current.normalized()
    current_result = None
    for r in results:
        if all(abs(r.weights.get(k, 0) - current_w.get(k, 0)) < 0.02 for k in FACTOR_NAMES):
            current_result = r
            break

    if current_result:
        print(f"\nCURRENT weights: top1={current_result.top1_rate:.1%} "
              f"top2={current_result.top2_rate:.1%} "
              f"top3={current_result.top3_rate:.1%} "
              f"score={current_result.score:.4f} "
              f"avgRank={current_result.avg_picks_needed:.2f}")

    print(f"\n{'#':>3} {'top1':>6} {'top2':>6} {'top3':>6} {'score':>7} {'avgR':>5} "
          f"{'mw':>4} │ {'time':>5} {'form':>5} {'prize':>5} {'cat':>5} {'post':>5} {'drv':>5} {'trk':>5}")
    print("-" * 100)

    for i, r in enumerate(sorted_results[:top_n], 1):
        w = r.weights
        print(
            f"{i:>3} {r.top1_rate:>5.1%} {r.top2_rate:>5.1%} {r.top3_rate:>5.1%} "
            f"{r.score:>7.4f} {r.avg_picks_needed:>5.2f} "
            f"{r.market_weight:>4.2f} │ "
            f"{w.get('time_analysis', 0):>5.2f} {w.get('form_curve', 0):>5.2f} "
            f"{w.get('prize_index', 0):>5.2f} {w.get('category_profile', 0):>5.2f} "
            f"{w.get('post_position', 0):>5.2f} {w.get('driver_trainer', 0):>5.2f} "
            f"{w.get('track_profile', 0):>5.2f}"
        )

    # Per-factor analysis: how much does each factor matter?
    print("\n\n=== FACTOR IMPORTANCE (isolated top-1 accuracy) ===\n")
    best = sorted_results[0]
    for factor in FACTOR_NAMES:
        # Test with this factor zeroed out
        w_without = dict(best.weights)
        w_without[factor] = 0.0
        total = sum(w_without.values())
        if total > 0:
            w_without = {k: v / total for k, v in w_without.items()}
        # This just shows what the best config looks like
        print(f"  {factor}: weight={best.weights.get(factor, 0):.3f}")


def analyze_market_only(races: list[RaceData]) -> None:
    """Benchmark: how well does market (streckprocent) alone predict?"""
    top1 = top2 = top3 = 0
    n = 0

    for race in races:
        if not race.winner or not race.bet_pcts:
            continue
        if sum(race.bet_pcts.values()) < 0.5:
            continue  # No real market data

        # Rank by bet percentage
        ranking = sorted(race.bet_pcts.keys(), key=lambda p: race.bet_pcts[p], reverse=True)
        winner = race.winner
        if winner not in ranking:
            continue

        n += 1
        rank = ranking.index(winner) + 1
        if rank <= 1:
            top1 += 1
        if rank <= 2:
            top2 += 1
        if rank <= 3:
            top3 += 1

    if n > 0:
        print(f"\nMARKET ONLY (streckprocent): {n} races with market data")
        print(f"  top1={top1/n:.1%}  top2={top2/n:.1%}  top3={top3/n:.1%}")


def analyze_model_only(races: list[RaceData], weights: dict[str, float]) -> None:
    """Benchmark: model without market blend."""
    result = evaluate_weights(races, weights, market_weight=0.0)
    print(f"\nMODEL ONLY (no market): {result.num_races} races")
    print(f"  top1={result.top1_rate:.1%}  top2={result.top2_rate:.1%}  top3={result.top3_rate:.1%}")


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Optimize factor weights")
    parser.add_argument("--game-types", default="V75,V85,V86,GS75",
                        help="Comma-separated game types")
    parser.add_argument("--step", type=float, default=0.05,
                        help="Grid search step size")
    parser.add_argument("--from-date", default="2015-01-01",
                        help="Start date")
    parser.add_argument("--to-date", default="2026-04-01",
                        help="End date")
    args = parser.parse_args()

    game_types = args.game_types.split(",")
    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)

    # Step 1: Load and score all races
    races = await load_all_races(game_types, start, end)

    if not races:
        print("No races found!")
        return

    # Step 2: Benchmarks
    print("\n=== BENCHMARKS ===")
    current_weights = FactorWeights().normalized()
    current = evaluate_weights(races, current_weights, 0.15)
    print(f"\nCURRENT CONFIG (v4 weights, 15% market):")
    print(f"  top1={current.top1_rate:.1%}  top2={current.top2_rate:.1%}  "
          f"top3={current.top3_rate:.1%}  avgRank={current.avg_picks_needed:.2f}")

    analyze_market_only(races)
    analyze_model_only(races, current_weights)

    # Step 3: Grid search
    results = grid_search(races, step=args.step)

    # Step 4: Print results
    print_results(results)

    # Step 5: Best config suggestion
    best = sorted(results, key=lambda r: r.score, reverse=True)[0]
    print("\n\n=== RECOMMENDED CONFIG ===\n")
    print("class FactorWeights:")
    for name in FACTOR_NAMES:
        print(f"    {name}: float = {best.weights.get(name, 0.05):.3f}")
    print(f"\nsuper_score_model_weight: float = {1.0 - best.market_weight:.2f}")
    print(f"\nExpected: top1={best.top1_rate:.1%}  top2={best.top2_rate:.1%}  top3={best.top3_rate:.1%}")

    # Improvement vs current
    improvement_top2 = best.top2_rate - current.top2_rate
    improvement_top3 = best.top3_rate - current.top3_rate
    print(f"Improvement: top2 {improvement_top2:+.1%}  top3 {improvement_top3:+.1%}")


if __name__ == "__main__":
    asyncio.run(main())
