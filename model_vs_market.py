#!/usr/bin/env python3
"""Jämför modellens predictions mot spelmarknadens streckprocent.

Kör CompositeAnalyzer på alla historiska V75+V85-omgångar och jämför:
- Model ranking (composite_score, högst först)
- Market ranking (bet_percentage / streckprocent, högst först)

Skriver ut detaljerade jämförelser: overall, per startmetod, per fältstorlek,
skrällar, favoriter, head-to-head och kalibrering.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import AnalysisConfig
from trav_agent.data.atg_client import ATGClient
from trav_agent.data.models import GameRound

import logging

logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def filter_future_starts(game_round: GameRound) -> None:
    """Remove starts that happened after the race date (prevent data leakage)."""
    for race in game_round.races:
        for entry in race.entries:
            entry.horse.past_starts = [
                s for s in entry.horse.past_starts if s.start_date < race.race_date
            ]


def compute_metrics(races: list[dict], label: str) -> dict:
    """Compute top-N hit rates and rank stats for a set of races.

    Each race dict has keys: model_ranking, market_ranking, winner, field_size
    """
    n = len(races)
    if n == 0:
        return {}

    model_top1 = model_top2 = model_top3 = model_top5 = 0
    market_top1 = market_top2 = market_top3 = market_top5 = 0
    model_ranks = []
    market_ranks = []

    for r in races:
        winner = r["winner"]
        mr = r["model_ranking"]
        mk = r["market_ranking"]

        # Model rank of winner
        if winner in mr:
            m_rank = mr.index(winner) + 1
        else:
            m_rank = len(mr) + 1
        model_ranks.append(m_rank)

        if m_rank <= 1:
            model_top1 += 1
        if m_rank <= 2:
            model_top2 += 1
        if m_rank <= 3:
            model_top3 += 1
        if m_rank <= 5:
            model_top5 += 1

        # Market rank of winner
        if winner in mk:
            k_rank = mk.index(winner) + 1
        else:
            k_rank = len(mk) + 1
        market_ranks.append(k_rank)

        if k_rank <= 1:
            market_top1 += 1
        if k_rank <= 2:
            market_top2 += 1
        if k_rank <= 3:
            market_top3 += 1
        if k_rank <= 5:
            market_top5 += 1

    return {
        "n": n,
        "model_top1": model_top1 / n,
        "model_top2": model_top2 / n,
        "model_top3": model_top3 / n,
        "model_top5": model_top5 / n,
        "model_avg_rank": statistics.mean(model_ranks),
        "model_median_rank": statistics.median(model_ranks),
        "market_top1": market_top1 / n,
        "market_top2": market_top2 / n,
        "market_top3": market_top3 / n,
        "market_top5": market_top5 / n,
        "market_avg_rank": statistics.mean(market_ranks),
        "market_median_rank": statistics.median(market_ranks),
        "model_ranks": model_ranks,
        "market_ranks": market_ranks,
    }


def print_metrics_table(metrics: dict, title: str):
    """Print a nicely formatted comparison table."""
    if not metrics:
        print(f"  (no data)")
        return

    n = metrics["n"]
    print(f"  Races: {n}")
    print(f"  {'Metric':<25} {'Model':>10} {'Market (streck)':>16}")
    print(f"  {'-'*25} {'-'*10} {'-'*16}")
    print(
        f"  {'Top-1 (rank 1 wins)':<25} {metrics['model_top1']:>9.1%} {metrics['market_top1']:>15.1%}"
    )
    print(
        f"  {'Top-2 (winner in top 2)':<25} {metrics['model_top2']:>9.1%} {metrics['market_top2']:>15.1%}"
    )
    print(
        f"  {'Top-3 (winner in top 3)':<25} {metrics['model_top3']:>9.1%} {metrics['market_top3']:>15.1%}"
    )
    print(
        f"  {'Top-5 (winner in top 5)':<25} {metrics['model_top5']:>9.1%} {metrics['market_top5']:>15.1%}"
    )
    print(
        f"  {'Avg rank of winner':<25} {metrics['model_avg_rank']:>10.2f} {metrics['market_avg_rank']:>16.2f}"
    )
    print(
        f"  {'Median rank of winner':<25} {metrics['model_median_rank']:>10.1f} {metrics['market_median_rank']:>16.1f}"
    )


async def main():
    start_time = time.time()

    # ── Fetch data (same as fine_tune_weights.py) ──────────────────────────
    client = ATGClient()
    all_rounds: list[GameRound] = []
    end = date(2026, 2, 21)

    for gt, start in [("V75", date(2024, 1, 1)), ("V85", date(2024, 3, 1))]:
        logger.info(f"Fetching {gt}...")
        async for day, gr in client.fetch_historical_rounds_iter(gt, start, end):
            if gr and gr.is_finished:
                all_rounds.append(gr)

    logger.info(f"Total: {len(all_rounds)} rounds")

    # ── Analyze all rounds and collect race data ───────────────────────────
    logger.info("Analyzing all rounds with CompositeAnalyzer...")
    all_races = []
    skipped_no_result = 0
    skipped_no_market = 0

    for gr in all_rounds:
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)
        analyzer = CompositeAnalyzer(AnalysisConfig())
        analyzer.analyze_round(gr_copy)

        for race in gr_copy.races:
            if not race.result_order or not race.active_entries:
                skipped_no_result += 1
                continue

            winner = race.result_order[0]

            # Check if market data is complete (all entries need bet_percentage)
            entries_with_bp = [
                e for e in race.active_entries if e.bet_percentage is not None
            ]
            if len(entries_with_bp) < len(race.active_entries):
                # Some entries lack market data — skip this race
                skipped_no_market += 1
                continue

            # Model ranking: sort by composite_score descending
            model_sorted = sorted(
                race.active_entries, key=lambda e: e.composite_score, reverse=True
            )
            model_ranking = [e.post_position for e in model_sorted]

            # Market ranking: sort by bet_percentage descending
            market_sorted = sorted(
                race.active_entries, key=lambda e: e.bet_percentage, reverse=True
            )
            market_ranking = [e.post_position for e in market_sorted]

            # Winner's bet_percentage
            winner_bp = None
            for e in race.active_entries:
                if e.post_position == winner:
                    winner_bp = e.bet_percentage
                    break

            all_races.append(
                {
                    "winner": winner,
                    "model_ranking": model_ranking,
                    "market_ranking": market_ranking,
                    "method": race.start_method.value,
                    "field_size": len(race.active_entries),
                    "winner_bp": winner_bp,
                    "game_type": gr.game_type,
                    "date": race.race_date.isoformat(),
                    "track": race.track_name,
                    "race_number": race.race_number,
                }
            )

    logger.info(
        f"Collected {len(all_races)} races with complete data "
        f"(skipped {skipped_no_result} no-result, {skipped_no_market} incomplete market)"
    )

    if not all_races:
        print("ERROR: No races with complete data found!")
        return

    # ══════════════════════════════════════════════════════════════════════════
    # A) OVERALL COMPARISON
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("A) OVERALL COMPARISON — Model vs Market (streckprocent)")
    print("=" * 70)
    overall = compute_metrics(all_races, "Overall")
    print_metrics_table(overall, "Overall")

    # ══════════════════════════════════════════════════════════════════════════
    # B) BY RACE TYPE (volt vs auto)
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("B) BY START METHOD")
    print("=" * 70)

    volt_races = [r for r in all_races if r["method"] == "volt"]
    auto_races = [r for r in all_races if r["method"] == "auto"]

    print(f"\n  --- VOLT ({len(volt_races)} races) ---")
    print_metrics_table(compute_metrics(volt_races, "Volt"), "Volt")

    print(f"\n  --- AUTO ({len(auto_races)} races) ---")
    print_metrics_table(compute_metrics(auto_races, "Auto"), "Auto")

    # ══════════════════════════════════════════════════════════════════════════
    # C) BY FIELD SIZE
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("C) BY FIELD SIZE")
    print("=" * 70)

    small = [r for r in all_races if r["field_size"] <= 8]
    medium = [r for r in all_races if 9 <= r["field_size"] <= 11]
    large = [r for r in all_races if r["field_size"] >= 12]

    print(f"\n  --- Small field (<=8 starters, {len(small)} races) ---")
    print_metrics_table(compute_metrics(small, "Small"), "Small")

    print(f"\n  --- Medium field (9-11 starters, {len(medium)} races) ---")
    print_metrics_table(compute_metrics(medium, "Medium"), "Medium")

    print(f"\n  --- Large field (12+ starters, {len(large)} races) ---")
    print_metrics_table(compute_metrics(large, "Large"), "Large")

    # ══════════════════════════════════════════════════════════════════════════
    # D) UPSET ANALYSIS (skrallar)
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("D) UPSET ANALYSIS (winner had <10% streck)")
    print("=" * 70)

    upsets = [r for r in all_races if r["winner_bp"] is not None and r["winner_bp"] < 0.10]

    if upsets:
        upset_metrics = compute_metrics(upsets, "Upsets")
        print(f"\n  Total upsets: {len(upsets)} / {len(all_races)} ({len(upsets)/len(all_races):.1%} of races)")
        print(f"\n  {'Metric':<30} {'Model':>10} {'Market (streck)':>16}")
        print(f"  {'-'*30} {'-'*10} {'-'*16}")
        print(
            f"  {'Avg rank of upset winner':<30} {upset_metrics['model_avg_rank']:>10.2f} {upset_metrics['market_avg_rank']:>16.2f}"
        )
        print(
            f"  {'Median rank of upset winner':<30} {upset_metrics['model_median_rank']:>10.1f} {upset_metrics['market_median_rank']:>16.1f}"
        )
        print(
            f"  {'Top-3 rate for upsets':<30} {upset_metrics['model_top3']:>9.1%} {upset_metrics['market_top3']:>15.1%}"
        )
        print(
            f"  {'Top-5 rate for upsets':<30} {upset_metrics['model_top5']:>9.1%} {upset_metrics['market_top5']:>15.1%}"
        )
    else:
        print("  No upsets found.")

    # ══════════════════════════════════════════════════════════════════════════
    # E) FAVORITE ANALYSIS
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("E) FAVORITE ANALYSIS (winner had >25% streck)")
    print("=" * 70)

    favorites = [r for r in all_races if r["winner_bp"] is not None and r["winner_bp"] > 0.25]

    if favorites:
        fav_metrics = compute_metrics(favorites, "Favorites")
        print(f"\n  Favorites won: {len(favorites)} / {len(all_races)} ({len(favorites)/len(all_races):.1%} of races)")
        print(f"\n  {'Metric':<30} {'Model':>10} {'Market (streck)':>16}")
        print(f"  {'-'*30} {'-'*10} {'-'*16}")
        print(
            f"  {'Top-1 rate for favorites':<30} {fav_metrics['model_top1']:>9.1%} {fav_metrics['market_top1']:>15.1%}"
        )
        print(
            f"  {'Top-2 rate for favorites':<30} {fav_metrics['model_top2']:>9.1%} {fav_metrics['market_top2']:>15.1%}"
        )
        print(
            f"  {'Avg rank of fav winner':<30} {fav_metrics['model_avg_rank']:>10.2f} {fav_metrics['market_avg_rank']:>16.2f}"
        )
    else:
        print("  No favorite winners found.")

    # ══════════════════════════════════════════════════════════════════════════
    # F) HEAD-TO-HEAD PER RACE
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("F) HEAD-TO-HEAD: Who ranked the winner higher?")
    print("=" * 70)

    model_wins = 0
    market_wins = 0
    ties = 0

    for r in all_races:
        winner = r["winner"]
        mr = r["model_ranking"]
        mk = r["market_ranking"]

        m_rank = mr.index(winner) + 1 if winner in mr else len(mr) + 1
        k_rank = mk.index(winner) + 1 if winner in mk else len(mk) + 1

        if m_rank < k_rank:
            model_wins += 1
        elif k_rank < m_rank:
            market_wins += 1
        else:
            ties += 1

    print(f"\n  Model ranked winner higher:  {model_wins:>4} races ({model_wins/len(all_races):.1%})")
    print(f"  Market ranked winner higher: {market_wins:>4} races ({market_wins/len(all_races):.1%})")
    print(f"  Tied (same rank):            {ties:>4} races ({ties/len(all_races):.1%})")
    print(f"  Model advantage (net):       {model_wins - market_wins:>+4} races")

    # ══════════════════════════════════════════════════════════════════════════
    # G) CALIBRATION: Rank vs actual win rate
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("G) CALIBRATION: Rank position vs actual win rate")
    print("=" * 70)

    # For model: for each rank 1..10, how often did that horse win?
    model_rank_wins = defaultdict(int)
    model_rank_total = defaultdict(int)
    market_rank_wins = defaultdict(int)
    market_rank_total = defaultdict(int)

    for r in all_races:
        winner = r["winner"]
        mr = r["model_ranking"]
        mk = r["market_ranking"]

        for rank_idx in range(min(10, len(mr))):
            model_rank_total[rank_idx + 1] += 1
            if mr[rank_idx] == winner:
                model_rank_wins[rank_idx + 1] += 1

        for rank_idx in range(min(10, len(mk))):
            market_rank_total[rank_idx + 1] += 1
            if mk[rank_idx] == winner:
                market_rank_wins[rank_idx + 1] += 1

    print(f"\n  {'Rank':<6} {'Model win%':>12} {'Market win%':>13} {'Delta (M-Mk)':>14}")
    print(f"  {'-'*6} {'-'*12} {'-'*13} {'-'*14}")
    for rank in range(1, 11):
        m_pct = (
            model_rank_wins[rank] / model_rank_total[rank]
            if model_rank_total[rank] > 0
            else 0
        )
        k_pct = (
            market_rank_wins[rank] / market_rank_total[rank]
            if market_rank_total[rank] > 0
            else 0
        )
        delta = m_pct - k_pct
        print(
            f"  {rank:<6} {m_pct:>11.1%} {k_pct:>12.1%} {delta:>+13.1%}"
        )

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Total races analyzed:  {len(all_races)}")
    print(f"  Total rounds:          {len(all_rounds)}")
    print(f"  Model Top-1:           {overall['model_top1']:.1%}")
    print(f"  Market Top-1:          {overall['market_top1']:.1%}")
    print(f"  Model Top-3:           {overall['model_top3']:.1%}")
    print(f"  Market Top-3:          {overall['market_top3']:.1%}")
    print(f"  Model avg rank:        {overall['model_avg_rank']:.2f}")
    print(f"  Market avg rank:       {overall['market_avg_rank']:.2f}")
    print(f"  Head-to-head:          Model {model_wins} — Market {market_wins} — Ties {ties}")
    elapsed = time.time() - start_time
    print(f"\n  Time: {elapsed:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
