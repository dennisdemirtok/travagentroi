#!/usr/bin/env python3
"""BREAKTHROUGH ANALYSIS — Deep V75/V85 profitability study.

Combines model composite_score with market streckprocent to find
the optimal super_score blend. Then tests adaptive pick strategies,
banker+oppen systems, and dynamic race selection to maximize hit rate
and ROI.

7 Steps:
1. Best super_score blend (model + market)
2. Adaptive picks per race (confidence-based allocation)
3. Per-round hittability analysis
4. Breaker race analysis
5. Banker + Oppen strategy
6. Dynamic Oppen selection
7. Final ROI calculation

Run: python3 breakthrough_analysis.py
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

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

# ── Constants ────────────────────────────────────────────────────────────────

ROW_PRICE = {"V75": 0.50, "V85": 1.00}
PRIZE_POOL_SHARE = 0.35
DEFAULT_POOL = {"V75": 30_000_000, "V85": 5_000_000}


def product(iterable):
    result = 1
    for x in iterable:
        result *= x
    return result


def filter_future_starts(game_round: GameRound) -> None:
    for race in game_round.races:
        for entry in race.entries:
            entry.horse.past_starts = [
                s for s in entry.horse.past_starts if s.start_date < race.race_date
            ]


def sep(title, char="=", width=100):
    print(f"\n{char * width}")
    print(f"  {title}")
    print(f"{char * width}")


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class EntryInfo:
    """Lightweight entry data for simulation."""
    post_position: int
    composite_score: float
    bet_percentage: float  # streckprocent
    factor_scores: dict
    recommendation: str
    horse_name: str


@dataclass
class RaceInfo:
    """Lightweight race data for simulation."""
    race_number: int
    track_name: str
    num_starters: int
    start_method: str
    distance: int
    winner_pp: int
    entries: list[EntryInfo]  # sorted by composite_score desc
    upset_risk: float
    gap_to_second: float
    gap_to_third: float


@dataclass
class RoundInfo:
    """Complete round for simulation."""
    game_type: str
    game_id: str
    round_date: date
    track_name: str
    turnover: Optional[int]
    jackpot: Optional[int]
    races: list[RaceInfo]


# ── Data Loading ─────────────────────────────────────────────────────────────

async def load_rounds() -> list[GameRound]:
    client = ATGClient()
    all_rounds: list[GameRound] = []
    end = date(2026, 2, 21)

    for gt, start in [("V75", date(2024, 1, 1)), ("V85", date(2024, 3, 1))]:
        logger.info(f"Loading {gt}...")
        async for day, gr in client.fetch_historical_rounds_iter(gt, start, end):
            if gr and gr.is_finished:
                all_rounds.append(gr)

    logger.info(f"Total loaded: {len(all_rounds)} rounds")
    return all_rounds


def analyze_all_rounds(all_rounds: list[GameRound]) -> list[RoundInfo]:
    """Analyze each round with CompositeAnalyzer and build RoundInfo."""
    round_infos = []
    skipped = 0

    for idx, gr in enumerate(all_rounds):
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)

        analyzer = CompositeAnalyzer(AnalysisConfig())
        analyzer.analyze_round(gr_copy)

        expected_races = 7 if gr.game_type == "V75" else 8

        valid = True
        for race in gr_copy.races:
            if not race.result_order or not race.active_entries:
                valid = False
                break
        if not valid or len(gr_copy.races) < expected_races:
            skipped += 1
            continue

        ri = RoundInfo(
            game_type=gr.game_type,
            game_id=gr.game_id,
            round_date=gr.round_date,
            track_name=gr.track_name,
            turnover=gr.turnover,
            jackpot=gr.jackpot,
            races=[],
        )

        for race in gr_copy.races:
            winner_pp = race.result_order[0]

            sorted_entries = sorted(
                race.active_entries,
                key=lambda e: e.composite_score,
                reverse=True,
            )

            # Check winner exists
            winner_found = any(e.post_position == winner_pp for e in sorted_entries)
            if not winner_found:
                valid = False
                break

            entries = []
            for e in sorted_entries:
                entries.append(EntryInfo(
                    post_position=e.post_position,
                    composite_score=e.composite_score,
                    bet_percentage=e.bet_percentage or 0.0,
                    factor_scores=dict(e.factor_scores),
                    recommendation=e.recommendation,
                    horse_name=e.horse.name,
                ))

            gap2 = 0.0
            gap3 = 0.0
            if len(sorted_entries) >= 2:
                gap2 = sorted_entries[0].composite_score - sorted_entries[1].composite_score
            if len(sorted_entries) >= 3:
                gap3 = sorted_entries[0].composite_score - sorted_entries[2].composite_score

            race_info = RaceInfo(
                race_number=race.race_number,
                track_name=race.track_name,
                num_starters=race.num_starters,
                start_method=race.start_method.value,
                distance=race.distance,
                winner_pp=winner_pp,
                entries=entries,
                upset_risk=race.upset_risk,
                gap_to_second=gap2,
                gap_to_third=gap3,
            )
            ri.races.append(race_info)

        if not valid or len(ri.races) < expected_races:
            skipped += 1
            continue

        round_infos.append(ri)

        if (idx + 1) % 20 == 0:
            logger.info(f"  Analyzed {idx+1}/{len(all_rounds)}...")

    logger.info(f"Total valid rounds: {len(round_infos)} (skipped {skipped})")
    return round_infos


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: BEST SUPER_SCORE BLEND
# ══════════════════════════════════════════════════════════════════════════════

def compute_super_scores(race: RaceInfo, alpha: float) -> list[tuple[int, float]]:
    """Compute super_score for each entry in a race.

    super_score = composite * (1-alpha) + market * alpha
    where market = bet_percentage * 100 (normalized to ~same scale as composite)
    """
    results = []
    for e in race.entries:
        model_score = e.composite_score
        market_score = e.bet_percentage * 100.0  # streck 0.25 -> 25

        # Scale market to be on similar scale as composite (0-100)
        # Market is already in a 0-100 range when multiplied by 100
        # But composite tends to cluster 20-90, market can be 1-40
        # We'll normalize market within the field to 0-100 range
        super_s = model_score * (1.0 - alpha) + market_score * alpha
        results.append((e.post_position, super_s))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


def compute_super_scores_normalized(race: RaceInfo, alpha: float) -> list[tuple[int, float]]:
    """Compute super_score with field-normalized market signal.

    Normalizes market (streck) to 0-100 within the field before blending.
    """
    # Get raw values
    composites = [(e.post_position, e.composite_score) for e in race.entries]
    markets_raw = [(e.post_position, e.bet_percentage) for e in race.entries]

    # Normalize market within field to 0-100
    market_vals = [m for _, m in markets_raw]
    min_m = min(market_vals) if market_vals else 0
    max_m = max(market_vals) if market_vals else 1
    spread = max_m - min_m if max_m > min_m else 1.0

    market_norm = {}
    for pp, m in markets_raw:
        market_norm[pp] = ((m - min_m) / spread) * 80 + 10  # scale to 10-90

    composite_dict = {pp: c for pp, c in composites}

    results = []
    for pp in composite_dict:
        model_s = composite_dict[pp]
        market_s = market_norm.get(pp, 50.0)
        super_s = model_s * (1.0 - alpha) + market_s * alpha
        results.append((pp, super_s))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


def step1_super_score(round_infos: list[RoundInfo]) -> dict:
    sep("STEP 1: BEST SUPER_SCORE BLEND (Model + Market)")

    all_races = []
    for ri in round_infos:
        for race in ri.races:
            all_races.append(race)

    print(f"\n  Total races: {len(all_races)}")
    print(f"  Total rounds: {len(round_infos)}")

    # Collect per-round info for round-level metrics
    v75_rounds = [ri for ri in round_infos if ri.game_type == "V75"]
    v85_rounds = [ri for ri in round_infos if ri.game_type == "V85"]

    print(f"  V75 rounds: {len(v75_rounds)}, V85 rounds: {len(v85_rounds)}")

    # Test alpha from 0.0 to 0.50 in steps of 0.02
    alphas = [round(a * 0.02, 2) for a in range(26)]  # 0.00 to 0.50

    results = []

    print(f"\n  {'Alpha':>6} {'Top-1':>7} {'Top-2':>7} {'Top-3':>7} | "
          f"{'V75 all-t2':>10} {'V75 all-t3':>10} | "
          f"{'V85 all-t2':>10} {'V85 all-t3':>10}")
    print(f"  {'-'*6} {'-'*7} {'-'*7} {'-'*7} | {'-'*10} {'-'*10} | {'-'*10} {'-'*10}")

    for alpha in alphas:
        top1 = top2 = top3 = 0
        n = 0

        # Per-round tracking
        round_hits = {}  # round_idx -> list of winner_ranks

        for ri_idx, ri in enumerate(round_infos):
            round_hits[ri_idx] = []
            for race in ri.races:
                ranked = compute_super_scores_normalized(race, alpha)
                ranking_pps = [pp for pp, _ in ranked]

                winner = race.winner_pp
                if winner in ranking_pps:
                    w_rank = ranking_pps.index(winner) + 1
                else:
                    w_rank = len(ranking_pps) + 1

                round_hits[ri_idx].append(w_rank)

                if w_rank == 1: top1 += 1
                if w_rank <= 2: top2 += 1
                if w_rank <= 3: top3 += 1
                n += 1

        # V75 round-level: all winners in top-2, top-3
        v75_all_t2 = 0
        v75_all_t3 = 0
        v85_all_t2 = 0
        v85_all_t3 = 0

        for ri_idx, ri in enumerate(round_infos):
            ranks = round_hits[ri_idx]
            all_t2 = all(r <= 2 for r in ranks)
            all_t3 = all(r <= 3 for r in ranks)

            if ri.game_type == "V75":
                if all_t2: v75_all_t2 += 1
                if all_t3: v75_all_t3 += 1
            else:
                if all_t2: v85_all_t2 += 1
                if all_t3: v85_all_t3 += 1

        result = {
            "alpha": alpha,
            "top1": top1 / n if n else 0,
            "top2": top2 / n if n else 0,
            "top3": top3 / n if n else 0,
            "v75_all_t2": v75_all_t2,
            "v75_all_t3": v75_all_t3,
            "v85_all_t2": v85_all_t2,
            "v85_all_t3": v85_all_t3,
            "v75_all_t2_pct": v75_all_t2 / len(v75_rounds) if v75_rounds else 0,
            "v75_all_t3_pct": v75_all_t3 / len(v75_rounds) if v75_rounds else 0,
            "v85_all_t2_pct": v85_all_t2 / len(v85_rounds) if v85_rounds else 0,
            "v85_all_t3_pct": v85_all_t3 / len(v85_rounds) if v85_rounds else 0,
            "round_hits": round_hits,
        }
        results.append(result)

        print(f"  {alpha:>6.2f} {result['top1']:>6.1%} {result['top2']:>6.1%} {result['top3']:>6.1%} | "
              f"{v75_all_t2:>4}/{len(v75_rounds):<4} {v75_all_t3:>4}/{len(v75_rounds):<4} | "
              f"{v85_all_t2:>4}/{len(v85_rounds):<4} {v85_all_t3:>4}/{len(v85_rounds):<4}")

    # Find best alpha for different objectives
    best_top1 = max(results, key=lambda r: r["top1"])
    best_top3 = max(results, key=lambda r: r["top3"])
    best_v75_t3 = max(results, key=lambda r: r["v75_all_t3"])
    best_v85_t3 = max(results, key=lambda r: r["v85_all_t3"])
    best_combined = max(results, key=lambda r: r["v75_all_t3"] + r["v85_all_t3"])

    print(f"\n  BEST ALPHA VALUES:")
    print(f"    Max Top-1 accuracy: alpha={best_top1['alpha']:.2f} -> {best_top1['top1']:.1%}")
    print(f"    Max Top-3 accuracy: alpha={best_top3['alpha']:.2f} -> {best_top3['top3']:.1%}")
    print(f"    Max V75 all-in-t3:  alpha={best_v75_t3['alpha']:.2f} -> {best_v75_t3['v75_all_t3']} rounds")
    print(f"    Max V85 all-in-t3:  alpha={best_v85_t3['alpha']:.2f} -> {best_v85_t3['v85_all_t3']} rounds")
    print(f"    Max combined t3:    alpha={best_combined['alpha']:.2f} -> {best_combined['v75_all_t3']+best_combined['v85_all_t3']} rounds")

    # Use the best combined alpha going forward
    best_alpha = best_combined["alpha"]
    print(f"\n  SELECTED ALPHA = {best_alpha:.2f}")

    # Serializable results (without round_hits which is huge)
    clean_results = []
    for r in results:
        cr = {k: v for k, v in r.items() if k != "round_hits"}
        clean_results.append(cr)

    return {
        "alpha_results": clean_results,
        "best_alpha": best_alpha,
        "best_top1_alpha": best_top1["alpha"],
        "best_top3_alpha": best_top3["alpha"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: ADAPTIVE PICKS PER RACE
# ══════════════════════════════════════════════════════════════════════════════

def compute_race_confidence(race: RaceInfo, alpha: float) -> float:
    """Compute a confidence score for a race.

    High confidence = easy to pick winner
    Low confidence = needs more picks (gardering)
    """
    ranked = compute_super_scores_normalized(race, alpha)
    if len(ranked) < 2:
        return 100.0

    top_score = ranked[0][1]
    second_score = ranked[1][1]
    third_score = ranked[2][1] if len(ranked) >= 3 else second_score

    gap_1_2 = top_score - second_score
    gap_1_3 = top_score - third_score

    # Market agreement: does market's #1 match our #1?
    market_sorted = sorted(race.entries, key=lambda e: e.bet_percentage, reverse=True)
    market_top_pp = market_sorted[0].post_position if market_sorted else -1
    our_top_pp = ranked[0][0]
    agreement_bonus = 15.0 if market_top_pp == our_top_pp else 0.0

    # Top horse's streck (higher = more certain)
    top_entry = next((e for e in race.entries if e.post_position == our_top_pp), None)
    top_streck = top_entry.bet_percentage if top_entry else 0.0
    streck_bonus = top_streck * 50  # 30% streck -> 15 points

    # Field size penalty
    field_penalty = max(0, (race.num_starters - 8)) * 3

    # Volt penalty
    volt_penalty = 5 if race.start_method == "volt" else 0

    confidence = gap_1_2 * 2 + gap_1_3 * 1 + agreement_bonus + streck_bonus - field_penalty - volt_penalty

    return max(0, min(100, confidence))


def adaptive_picks_for_round(ri: RoundInfo, alpha: float, budget: float) -> list[int]:
    """Compute adaptive number of picks per race within budget.

    Returns list of pick counts per race.
    """
    row_price = ROW_PRICE.get(ri.game_type, 1.0)
    max_rows = int(budget / row_price)
    n_races = len(ri.races)

    # Compute confidence for each race
    confidences = []
    for race in ri.races:
        conf = compute_race_confidence(race, alpha)
        confidences.append(conf)

    # Sort races by confidence (highest first)
    race_indices_by_conf = sorted(range(n_races), key=lambda i: confidences[i], reverse=True)

    # Start with 1 pick for each race
    picks = [1] * n_races

    # Expand least confident races first, within budget
    # Priority: expand the race with lowest confidence that hasn't been fully expanded
    while True:
        current_rows = product(picks)
        if current_rows >= max_rows:
            break

        expanded = False
        # Try to expand least confident race first
        for idx in reversed(race_indices_by_conf):
            race = ri.races[idx]
            if picks[idx] >= race.num_starters:
                continue

            new_picks = picks[:]
            new_picks[idx] += 1
            new_rows = product(new_picks)

            if new_rows <= max_rows:
                picks[idx] += 1
                expanded = True
                break

        if not expanded:
            break

    return picks


def step2_adaptive_picks(round_infos: list[RoundInfo], best_alpha: float) -> dict:
    sep("STEP 2: ADAPTIVE PICKS PER RACE")

    budgets = [500, 1000, 2000, 5000, 10000]

    results_by_budget = {}

    for budget in budgets:
        hits = 0
        total_cost = 0.0
        details = []

        for ri in round_infos:
            row_price = ROW_PRICE.get(ri.game_type, 1.0)
            picks = adaptive_picks_for_round(ri, best_alpha, budget)

            total_rows = product(picks)
            cost = total_rows * row_price
            total_cost += cost

            # Check if we hit: for each race, is the winner in our top-N picks?
            hit = True
            for i, race in enumerate(ri.races):
                ranked = compute_super_scores_normalized(race, best_alpha)
                our_picks_pps = [pp for pp, _ in ranked[:picks[i]]]

                if race.winner_pp not in our_picks_pps:
                    hit = False
                    break

            if hit:
                hits += 1

            details.append({
                "date": ri.round_date.isoformat(),
                "game_type": ri.game_type,
                "picks": picks,
                "rows": total_rows,
                "cost": cost,
                "hit": hit,
            })

        hit_rate = hits / len(round_infos) if round_infos else 0

        results_by_budget[budget] = {
            "hits": hits,
            "total_rounds": len(round_infos),
            "hit_rate": hit_rate,
            "total_cost": total_cost,
            "avg_cost": total_cost / len(round_infos) if round_infos else 0,
            "details": details,
        }

        print(f"\n  Budget {budget:>6} kr: {hits}/{len(round_infos)} hits ({hit_rate:.1%}), "
              f"avg cost={total_cost/len(round_infos):.0f} kr/round")

        # Show pick distribution
        all_picks = [d["picks"] for d in details]
        avg_picks = [statistics.mean([p[i] for p in all_picks]) for i in range(len(all_picks[0]))] if all_picks else []
        if avg_picks:
            print(f"    Avg picks per race: {', '.join(f'{p:.1f}' for p in avg_picks)}")
            print(f"    Avg rows: {statistics.mean([d['rows'] for d in details]):.0f}")

    return results_by_budget


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: PER-ROUND HITTABILITY ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def step3_hittability(round_infos: list[RoundInfo], best_alpha: float) -> dict:
    sep("STEP 3: PER-ROUND HITTABILITY ANALYSIS")

    round_analysis = []

    for ri in round_infos:
        race_details = []
        worst_rank = 0
        breaker_race_idx = -1

        for i, race in enumerate(ri.races):
            ranked = compute_super_scores_normalized(race, best_alpha)
            ranking_pps = [pp for pp, _ in ranked]

            winner = race.winner_pp
            if winner in ranking_pps:
                w_rank = ranking_pps.index(winner) + 1
            else:
                w_rank = len(ranking_pps) + 1

            if w_rank > worst_rank:
                worst_rank = w_rank
                breaker_race_idx = i

            # Winner entry info
            winner_entry = next((e for e in race.entries if e.post_position == winner), None)
            winner_streck = winner_entry.bet_percentage if winner_entry else 0.0
            winner_composite = winner_entry.composite_score if winner_entry else 0.0

            race_details.append({
                "race_number": race.race_number,
                "winner_pp": winner,
                "winner_name": winner_entry.horse_name if winner_entry else "?",
                "winner_super_rank": w_rank,
                "winner_streck": winner_streck,
                "winner_composite": winner_composite,
                "num_starters": race.num_starters,
                "start_method": race.start_method,
                "gap_to_second": race.gap_to_second,
                "upset_risk": race.upset_risk,
                "confidence": compute_race_confidence(race, best_alpha),
            })

        all_in_top2 = all(rd["winner_super_rank"] <= 2 for rd in race_details)
        all_in_top3 = all(rd["winner_super_rank"] <= 3 for rd in race_details)

        # Payout estimation
        winner_strecks = [max(rd["winner_streck"], 0.001) for rd in race_details]
        streck_product = product(winner_strecks)
        pool = ri.turnover if ri.turnover else DEFAULT_POOL.get(ri.game_type, 10_000_000)
        prize_pool = pool * PRIZE_POOL_SHARE
        row_price = ROW_PRICE.get(ri.game_type, 1.0)
        pool_rows = pool / row_price
        expected_winners = pool_rows * streck_product
        if expected_winners < 0.001:
            expected_winners = 0.001
        est_payout = min(prize_pool, prize_pool / expected_winners)

        round_analysis.append({
            "date": ri.round_date.isoformat(),
            "game_type": ri.game_type,
            "track": ri.track_name,
            "all_in_top2": all_in_top2,
            "all_in_top3": all_in_top3,
            "worst_rank": worst_rank,
            "breaker_race_idx": breaker_race_idx,
            "races": race_details,
            "estimated_payout": est_payout,
        })

    # Summary
    n = len(round_analysis)
    n_all_t2 = sum(1 for r in round_analysis if r["all_in_top2"])
    n_all_t3 = sum(1 for r in round_analysis if r["all_in_top3"])

    print(f"\n  Total rounds: {n}")
    print(f"  All winners in top-2 (super_score): {n_all_t2} ({n_all_t2/n:.1%})")
    print(f"  All winners in top-3 (super_score): {n_all_t3} ({n_all_t3/n:.1%})")

    # Distribution of worst_rank per round
    worst_ranks = [r["worst_rank"] for r in round_analysis]
    rank_dist = defaultdict(int)
    for wr in worst_ranks:
        rank_dist[wr] += 1

    print(f"\n  Distribution of WORST winner rank per round:")
    for rank in sorted(rank_dist.keys()):
        count = rank_dist[rank]
        bar = "#" * count
        print(f"    Rank {rank:>2}: {count:>3} rounds ({count/n:.1%}) {bar}")

    # Show the hittable rounds at different budgets with adaptive picks
    for budget in [2000, 5000]:
        hits = 0
        total_cost = 0
        for ri_idx, ri in enumerate(round_infos):
            row_price = ROW_PRICE.get(ri.game_type, 1.0)
            picks = adaptive_picks_for_round(ri, best_alpha, budget)
            total_rows = product(picks)
            cost = total_rows * row_price
            total_cost += cost

            hit = True
            for i, race in enumerate(ri.races):
                ranked = compute_super_scores_normalized(race, best_alpha)
                our_picks = [pp for pp, _ in ranked[:picks[i]]]
                if race.winner_pp not in our_picks:
                    hit = False
                    break
            if hit:
                hits += 1

        print(f"\n  Adaptive @ {budget}kr: {hits}/{n} hits ({hits/n:.1%}), "
              f"total cost={total_cost:,.0f}, avg={total_cost/n:.0f}/round")

    return {
        "round_analysis": round_analysis,
        "n_all_top2": n_all_t2,
        "n_all_top3": n_all_t3,
        "n_total": n,
    }


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: BREAKER RACE ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def step4_breaker_analysis(round_infos: list[RoundInfo], round_analysis: list[dict], best_alpha: float) -> dict:
    sep("STEP 4: BREAKER RACE ANALYSIS")

    # A breaker is the race that prevents a hit (worst rank > selection)
    # For top-3 system: breaker is race where winner rank > 3

    breakers_t3 = []
    for ra in round_analysis:
        if ra["all_in_top3"]:
            continue
        # Find the worst race
        worst_race = max(ra["races"], key=lambda r: r["winner_super_rank"])
        breakers_t3.append({
            "round_date": ra["date"],
            "game_type": ra["game_type"],
            "race_number": worst_race["race_number"],
            "winner_rank": worst_race["winner_super_rank"],
            "winner_streck": worst_race["winner_streck"],
            "winner_name": worst_race["winner_name"],
            "num_starters": worst_race["num_starters"],
            "start_method": worst_race["start_method"],
            "gap_to_second": worst_race["gap_to_second"],
            "upset_risk": worst_race["upset_risk"],
            "confidence": worst_race["confidence"],
        })

    print(f"\n  Breaker races (winner outside top-3): {len(breakers_t3)}")

    if breakers_t3:
        avg_rank = statistics.mean([b["winner_rank"] for b in breakers_t3])
        avg_streck = statistics.mean([b["winner_streck"] for b in breakers_t3 if b["winner_streck"] > 0])
        avg_starters = statistics.mean([b["num_starters"] for b in breakers_t3])
        avg_upset = statistics.mean([b["upset_risk"] for b in breakers_t3])
        avg_confidence = statistics.mean([b["confidence"] for b in breakers_t3])

        print(f"\n  BREAKER CHARACTERISTICS:")
        print(f"    Avg winner rank:     {avg_rank:.1f}")
        print(f"    Avg winner streck:   {avg_streck:.1%}")
        print(f"    Avg num starters:    {avg_starters:.1f}")
        print(f"    Avg upset_risk:      {avg_upset:.1f}")
        print(f"    Avg confidence:      {avg_confidence:.1f}")

        # Start method distribution
        method_dist = defaultdict(int)
        for b in breakers_t3:
            method_dist[b["start_method"]] += 1
        print(f"    Start method dist:   {dict(method_dist)}")

        # Starters distribution
        starters_dist = defaultdict(int)
        for b in breakers_t3:
            bucket = f"{(b['num_starters']//4)*4+1}-{(b['num_starters']//4)*4+4}"
            starters_dist[bucket] += 1
        print(f"    Starters dist:       {dict(sorted(starters_dist.items()))}")

        # Winner streck distribution
        streck_dist = {"<5%": 0, "5-10%": 0, "10-15%": 0, "15-25%": 0, ">25%": 0}
        for b in breakers_t3:
            s = b["winner_streck"]
            if s < 0.05: streck_dist["<5%"] += 1
            elif s < 0.10: streck_dist["5-10%"] += 1
            elif s < 0.15: streck_dist["10-15%"] += 1
            elif s < 0.25: streck_dist["15-25%"] += 1
            else: streck_dist[">25%"] += 1
        print(f"    Winner streck dist:  {streck_dist}")

        # Confidence distribution of breakers
        low_conf = sum(1 for b in breakers_t3 if b["confidence"] < 20)
        med_conf = sum(1 for b in breakers_t3 if 20 <= b["confidence"] < 40)
        high_conf = sum(1 for b in breakers_t3 if b["confidence"] >= 40)
        print(f"    Confidence dist:     low(<20)={low_conf}, med(20-40)={med_conf}, high(>40)={high_conf}")

        # KEY INSIGHT: What if we went "oppen" on the breaker race?
        print(f"\n  KEY INSIGHT: If we knew which race would be the breaker:")
        print(f"    -> Pick ALL starters on that race, top-2 on rest")
        print(f"    -> We would hit {len(round_analysis) - len([ra for ra in round_analysis if sum(1 for r in ra['races'] if r['winner_super_rank'] > 3) > 1])}/{len(round_analysis)} rounds")

        # Can we PREDICT the breaker?
        print(f"\n  CAN WE PREDICT THE BREAKER?")
        print(f"    If we pick the LOWEST confidence race as oppen:")

        correct_predictions = 0
        for ra in round_analysis:
            if ra["all_in_top3"]:
                continue
            # Our prediction: lowest confidence race is the breaker
            confs = [(r["race_number"], r["confidence"]) for r in ra["races"]]
            predicted_breaker = min(confs, key=lambda x: x[1])[0]
            actual_breaker = max(ra["races"], key=lambda r: r["winner_super_rank"])["race_number"]
            if predicted_breaker == actual_breaker:
                correct_predictions += 1

        if breakers_t3:
            print(f"      Correct predictions: {correct_predictions}/{len(breakers_t3)} ({correct_predictions/len(breakers_t3):.1%})")

    return {"breakers_t3": breakers_t3}


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5: BANKER + OPPEN STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

def simulate_oppen_strategy(ri: RoundInfo, best_alpha: float, n_oppen: int,
                            oppen_selector: str = "lowest_confidence",
                            base_picks: int = 2) -> dict:
    """Simulate a banker+oppen strategy on a round.

    n_oppen: number of races to go "oppen" (all starters)
    oppen_selector: method to choose which races are oppen
    base_picks: number of picks on non-oppen races
    """
    row_price = ROW_PRICE.get(ri.game_type, 1.0)
    n_races = len(ri.races)

    # Compute confidence for each race
    confidences = []
    for race in ri.races:
        conf = compute_race_confidence(race, best_alpha)
        confidences.append(conf)

    # Select oppen races based on selector
    if oppen_selector == "lowest_confidence":
        # Oppen on lowest confidence races
        sorted_indices = sorted(range(n_races), key=lambda i: confidences[i])
        oppen_indices = set(sorted_indices[:n_oppen])
    elif oppen_selector == "most_starters":
        sorted_indices = sorted(range(n_races), key=lambda i: ri.races[i].num_starters, reverse=True)
        oppen_indices = set(sorted_indices[:n_oppen])
    elif oppen_selector == "smallest_gap":
        sorted_indices = sorted(range(n_races), key=lambda i: ri.races[i].gap_to_second)
        oppen_indices = set(sorted_indices[:n_oppen])
    elif oppen_selector == "volt_races":
        volt_indices = [i for i in range(n_races) if ri.races[i].start_method == "volt"]
        # Sort by confidence ascending
        volt_indices.sort(key=lambda i: confidences[i])
        oppen_indices = set(volt_indices[:n_oppen])
        # If not enough volt races, fill with lowest confidence
        if len(oppen_indices) < n_oppen:
            remaining = sorted(range(n_races), key=lambda i: confidences[i])
            for idx in remaining:
                if idx not in oppen_indices:
                    oppen_indices.add(idx)
                    if len(oppen_indices) >= n_oppen:
                        break
    elif oppen_selector == "lowest_top1_streck":
        # Oppen on races where our top pick has lowest market confidence
        top1_strecks = []
        for i, race in enumerate(ri.races):
            ranked = compute_super_scores_normalized(race, best_alpha)
            top_pp = ranked[0][0]
            top_entry = next((e for e in race.entries if e.post_position == top_pp), None)
            top1_strecks.append(top_entry.bet_percentage if top_entry else 0)
        sorted_indices = sorted(range(n_races), key=lambda i: top1_strecks[i])
        oppen_indices = set(sorted_indices[:n_oppen])
    elif oppen_selector == "lowest_gap_x_streck":
        # Combined: lowest (gap * streck_of_#1)
        scores_for_selector = []
        for i, race in enumerate(ri.races):
            ranked = compute_super_scores_normalized(race, best_alpha)
            top_pp = ranked[0][0]
            top_entry = next((e for e in race.entries if e.post_position == top_pp), None)
            top_streck = top_entry.bet_percentage if top_entry else 0
            combined = race.gap_to_second * max(top_streck, 0.01)
            scores_for_selector.append(combined)
        sorted_indices = sorted(range(n_races), key=lambda i: scores_for_selector[i])
        oppen_indices = set(sorted_indices[:n_oppen])
    elif oppen_selector == "spread_streck":
        # Races where winner_streck is most spread out (std of streckprocent)
        streck_stds = []
        for i, race in enumerate(ri.races):
            strecks = [e.bet_percentage for e in race.entries if e.bet_percentage > 0]
            streck_stds.append(statistics.stdev(strecks) if len(strecks) >= 2 else 0)
        # LOW spread = uncertain (everyone close) -> oppen
        sorted_indices = sorted(range(n_races), key=lambda i: streck_stds[i])
        oppen_indices = set(sorted_indices[:n_oppen])
    else:
        oppen_indices = set()

    # Build picks
    picks_per_race = []
    for i, race in enumerate(ri.races):
        if i in oppen_indices:
            picks_per_race.append(race.num_starters)
        else:
            ranked = compute_super_scores_normalized(race, best_alpha)
            n_picks = min(base_picks, race.num_starters)
            picks_per_race.append(n_picks)

    total_rows = product(picks_per_race)
    cost = total_rows * row_price

    # Check hit
    hit = True
    for i, race in enumerate(ri.races):
        if i in oppen_indices:
            continue  # all starters selected, always hit
        ranked = compute_super_scores_normalized(race, best_alpha)
        our_picks = [pp for pp, _ in ranked[:picks_per_race[i]]]
        if race.winner_pp not in our_picks:
            hit = False
            break

    return {
        "picks": picks_per_race,
        "total_rows": total_rows,
        "cost": cost,
        "hit": hit,
        "oppen_indices": list(oppen_indices),
    }


def step5_banker_oppen(round_infos: list[RoundInfo], best_alpha: float) -> dict:
    sep("STEP 5: BANKER + OPPEN STRATEGY")

    print(f"\n  Strategy: pick top-N on confident races, ALL starters on uncertain races")

    results = {}

    for base_picks in [1, 2, 3]:
        results[f"base_{base_picks}"] = {}
        print(f"\n  === BASE PICKS = {base_picks} (on non-oppen races) ===")
        print(f"  {'N_oppen':>8} {'Hits':>6} {'Hit%':>7} {'Avg Cost':>10} {'Avg Rows':>10}")
        print(f"  {'-'*8} {'-'*6} {'-'*7} {'-'*10} {'-'*10}")

        for n_oppen in range(0, 5):  # 0 to 4 oppen races
            total_cost = 0
            total_rows_sum = 0
            hits = 0

            for ri in round_infos:
                result = simulate_oppen_strategy(ri, best_alpha, n_oppen,
                                                  "lowest_confidence", base_picks)
                total_cost += result["cost"]
                total_rows_sum += result["total_rows"]
                if result["hit"]:
                    hits += 1

            n = len(round_infos)
            avg_cost = total_cost / n if n else 0
            avg_rows = total_rows_sum / n if n else 0
            hit_rate = hits / n if n else 0

            results[f"base_{base_picks}"][n_oppen] = {
                "hits": hits,
                "hit_rate": hit_rate,
                "avg_cost": avg_cost,
                "avg_rows": avg_rows,
                "total_cost": total_cost,
            }

            print(f"  {n_oppen:>8} {hits:>6} {hit_rate:>6.1%} {avg_cost:>10,.0f} {avg_rows:>10,.0f}")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6: DYNAMIC OPPEN SELECTION
# ══════════════════════════════════════════════════════════════════════════════

def step6_dynamic_oppen(round_infos: list[RoundInfo], best_alpha: float) -> dict:
    sep("STEP 6: DYNAMIC OPPEN SELECTION")

    selectors = [
        "lowest_confidence",
        "most_starters",
        "smallest_gap",
        "volt_races",
        "lowest_top1_streck",
        "lowest_gap_x_streck",
        "spread_streck",
    ]

    results = {}

    for base_picks in [2, 3]:
        for n_oppen in [1, 2, 3]:
            print(f"\n  base_picks={base_picks}, n_oppen={n_oppen}:")
            print(f"  {'Selector':<25} {'Hits':>5} {'Hit%':>7} {'Avg Cost':>10} {'Avg Rows':>10}")
            print(f"  {'-'*25} {'-'*5} {'-'*7} {'-'*10} {'-'*10}")

            for selector in selectors:
                total_cost = 0
                total_rows_sum = 0
                hits = 0

                for ri in round_infos:
                    result = simulate_oppen_strategy(ri, best_alpha, n_oppen,
                                                      selector, base_picks)
                    total_cost += result["cost"]
                    total_rows_sum += result["total_rows"]
                    if result["hit"]:
                        hits += 1

                n = len(round_infos)
                avg_cost = total_cost / n if n else 0
                avg_rows = total_rows_sum / n if n else 0
                hit_rate = hits / n if n else 0

                key = f"b{base_picks}_o{n_oppen}_{selector}"
                results[key] = {
                    "base_picks": base_picks,
                    "n_oppen": n_oppen,
                    "selector": selector,
                    "hits": hits,
                    "hit_rate": hit_rate,
                    "avg_cost": avg_cost,
                    "avg_rows": avg_rows,
                    "total_cost": total_cost,
                }

                print(f"  {selector:<25} {hits:>5} {hit_rate:>6.1%} {avg_cost:>10,.0f} {avg_rows:>10,.0f}")

    # Find best strategy
    best_key = max(results.keys(), key=lambda k: results[k]["hit_rate"])
    best = results[best_key]
    print(f"\n  BEST STRATEGY: {best_key}")
    print(f"    Hits: {best['hits']}/{len(round_infos)} ({best['hit_rate']:.1%})")
    print(f"    Avg cost: {best['avg_cost']:,.0f} kr/round")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# STEP 7: FINAL ROI CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

def step7_roi(round_infos: list[RoundInfo], round_analysis: list[dict],
              best_alpha: float, step6_results: dict) -> dict:
    sep("STEP 7: FINAL ROI CALCULATION")

    # Find the best strategies from step 6 and compute full ROI
    # Sort all strategies by hit rate
    sorted_strategies = sorted(step6_results.values(),
                               key=lambda s: s["hit_rate"], reverse=True)

    # Top 10 strategies
    print(f"\n  TOP 10 STRATEGIES BY HIT RATE:")
    print(f"  {'Rank':>4} {'Strategy':>35} {'Hits':>5} {'Hit%':>7} {'Avg Cost':>10}")
    print(f"  {'-'*4} {'-'*35} {'-'*5} {'-'*7} {'-'*10}")

    for i, s in enumerate(sorted_strategies[:10]):
        desc = f"b{s['base_picks']}_o{s['n_oppen']}_{s['selector']}"
        print(f"  {i+1:>4} {desc:>35} {s['hits']:>5} {s['hit_rate']:>6.1%} {s['avg_cost']:>10,.0f}")

    # Now compute full ROI for top strategies
    sep("FULL ROI SIMULATION", "-")

    # For each top strategy, simulate with actual payout estimation
    roi_results = []

    for strategy in sorted_strategies[:15]:
        base_picks = strategy["base_picks"]
        n_oppen = strategy["n_oppen"]
        selector = strategy["selector"]

        total_cost = 0.0
        total_payout = 0.0
        hits = 0
        hit_details = []

        for ri_idx, ri in enumerate(round_infos):
            result = simulate_oppen_strategy(ri, best_alpha, n_oppen, selector, base_picks)
            cost = result["cost"]
            total_cost += cost

            if result["hit"]:
                hits += 1
                # Estimate payout
                ra = round_analysis[ri_idx]
                est_payout = ra["estimated_payout"]
                total_payout += est_payout
                hit_details.append({
                    "date": ri.round_date.isoformat(),
                    "game_type": ri.game_type,
                    "cost": cost,
                    "payout": est_payout,
                    "profit": est_payout - cost,
                })

        roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
        netto = total_payout - total_cost

        roi_results.append({
            "strategy": f"b{base_picks}_o{n_oppen}_{selector}",
            "base_picks": base_picks,
            "n_oppen": n_oppen,
            "selector": selector,
            "hits": hits,
            "total_rounds": len(round_infos),
            "hit_rate": hits / len(round_infos) if round_infos else 0,
            "total_cost": total_cost,
            "total_payout": total_payout,
            "netto": netto,
            "roi": roi,
            "avg_cost_per_round": total_cost / len(round_infos) if round_infos else 0,
            "avg_payout_per_hit": total_payout / hits if hits > 0 else 0,
            "hit_details": hit_details,
        })

    # Sort by ROI
    roi_results.sort(key=lambda r: r["roi"], reverse=True)

    print(f"\n  {'Strategy':>35} {'Hits':>5} {'Hit%':>7} {'Cost':>12} {'Payout':>12} {'Netto':>12} {'ROI':>8}")
    print(f"  {'-'*35} {'-'*5} {'-'*7} {'-'*12} {'-'*12} {'-'*12} {'-'*8}")

    for r in roi_results:
        print(f"  {r['strategy']:>35} {r['hits']:>5} {r['hit_rate']:>6.1%} "
              f"{r['total_cost']:>12,.0f} {r['total_payout']:>12,.0f} "
              f"{r['netto']:>+12,.0f} {r['roi']*100:>+7.1f}%")

    # Per V75 / V85 breakdown for best strategy
    if roi_results:
        best = roi_results[0]
        sep(f"BEST STRATEGY DETAILS: {best['strategy']}", "-")

        print(f"\n  Overall: {best['hits']}/{best['total_rounds']} hits, "
              f"ROI={best['roi']*100:+.1f}%, Netto={best['netto']:+,.0f} kr")
        print(f"  Avg cost/round: {best['avg_cost_per_round']:,.0f} kr")
        print(f"  Avg payout/hit: {best['avg_payout_per_hit']:,.0f} kr")

        # V75 vs V85 breakdown
        v75_rounds = [ri for ri in round_infos if ri.game_type == "V75"]
        v85_rounds = [ri for ri in round_infos if ri.game_type == "V85"]

        for gt, gt_rounds in [("V75", v75_rounds), ("V85", v85_rounds)]:
            if not gt_rounds:
                continue

            gt_cost = 0
            gt_payout = 0
            gt_hits = 0

            for ri in gt_rounds:
                result = simulate_oppen_strategy(
                    ri, best_alpha, best["n_oppen"], best["selector"], best["base_picks"])
                gt_cost += result["cost"]
                if result["hit"]:
                    gt_hits += 1
                    ri_idx = round_infos.index(ri)
                    gt_payout += round_analysis[ri_idx]["estimated_payout"]

            gt_roi = (gt_payout - gt_cost) / gt_cost if gt_cost > 0 else 0
            print(f"\n  {gt}: {gt_hits}/{len(gt_rounds)} hits ({gt_hits/len(gt_rounds):.1%}), "
                  f"cost={gt_cost:,.0f}, payout={gt_payout:,.0f}, "
                  f"netto={gt_payout-gt_cost:+,.0f}, ROI={gt_roi*100:+.1f}%")

        # Monthly breakdown
        sep("MONTHLY BREAKDOWN (Best Strategy)", "-")

        monthly = defaultdict(lambda: {"cost": 0, "payout": 0, "hits": 0, "rounds": 0})

        for ri_idx, ri in enumerate(round_infos):
            month_key = ri.round_date.strftime("%Y-%m")
            result = simulate_oppen_strategy(
                ri, best_alpha, best["n_oppen"], best["selector"], best["base_picks"])
            monthly[month_key]["cost"] += result["cost"]
            monthly[month_key]["rounds"] += 1
            if result["hit"]:
                monthly[month_key]["hits"] += 1
                monthly[month_key]["payout"] += round_analysis[ri_idx]["estimated_payout"]

        print(f"\n  {'Month':>8} {'Rounds':>7} {'Hits':>5} {'Cost':>10} {'Payout':>12} {'Netto':>12} {'ROI':>8}")
        print(f"  {'-'*8} {'-'*7} {'-'*5} {'-'*10} {'-'*12} {'-'*12} {'-'*8}")

        running_netto = 0
        for month in sorted(monthly.keys()):
            m = monthly[month]
            netto = m["payout"] - m["cost"]
            roi = (m["payout"] - m["cost"]) / m["cost"] if m["cost"] > 0 else 0
            running_netto += netto
            print(f"  {month:>8} {m['rounds']:>7} {m['hits']:>5} {m['cost']:>10,.0f} "
                  f"{m['payout']:>12,.0f} {netto:>+12,.0f} {roi*100:>+7.1f}%")

        print(f"\n  Running netto after all months: {running_netto:+,.0f} kr")

    # Also test: what about SELECTIVE play (only play certain rounds)?
    sep("SELECTIVE PLAY — Only play rounds with favorable characteristics", "-")

    if roi_results:
        best = roi_results[0]

        # Various filters
        filters = {
            "ALL": lambda ri, ra: True,
            "low_upset_risk": lambda ri, ra: statistics.mean([r["upset_risk"] for r in ra["races"]]) < 35,
            "high_confidence": lambda ri, ra: statistics.mean([r["confidence"] for r in ra["races"]]) > 25,
            "worst_rank_lte_5": lambda ri, ra: ra["worst_rank"] <= 5,
            "worst_rank_lte_6": lambda ri, ra: ra["worst_rank"] <= 6,
            "V75_only": lambda ri, ra: ri.game_type == "V75",
            "V85_only": lambda ri, ra: ri.game_type == "V85",
            "few_starters": lambda ri, ra: statistics.mean([r["num_starters"] for r in ra["races"]]) < 10,
        }

        print(f"\n  Using best strategy: {best['strategy']}")
        print(f"  {'Filter':<25} {'Played':>7} {'Hits':>5} {'Hit%':>7} {'Cost':>10} {'Payout':>12} {'ROI':>8}")
        print(f"  {'-'*25} {'-'*7} {'-'*5} {'-'*7} {'-'*10} {'-'*12} {'-'*8}")

        selective_results = {}
        for filt_name, filt_fn in filters.items():
            filtered_indices = [
                i for i in range(len(round_infos))
                if filt_fn(round_infos[i], round_analysis[i])
            ]

            if not filtered_indices:
                continue

            total_cost = 0
            total_payout = 0
            hits = 0

            for idx in filtered_indices:
                ri = round_infos[idx]
                result = simulate_oppen_strategy(
                    ri, best_alpha, best["n_oppen"], best["selector"], best["base_picks"])
                total_cost += result["cost"]
                if result["hit"]:
                    hits += 1
                    total_payout += round_analysis[idx]["estimated_payout"]

            roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
            hit_rate = hits / len(filtered_indices) if filtered_indices else 0

            selective_results[filt_name] = {
                "played": len(filtered_indices),
                "hits": hits,
                "hit_rate": hit_rate,
                "total_cost": total_cost,
                "total_payout": total_payout,
                "roi": roi,
            }

            print(f"  {filt_name:<25} {len(filtered_indices):>7} {hits:>5} {hit_rate:>6.1%} "
                  f"{total_cost:>10,.0f} {total_payout:>12,.0f} {roi*100:>+7.1f}%")

    # ── THE VERDICT ──
    sep("THE FINAL VERDICT", "=")

    if roi_results:
        positive_strategies = [r for r in roi_results if r["roi"] > 0]

        if positive_strategies:
            print(f"\n  POSITIVE ROI STRATEGIES FOUND: {len(positive_strategies)}")
            print()
            for i, r in enumerate(positive_strategies[:5]):
                print(f"  {i+1}. {r['strategy']}: ROI={r['roi']*100:+.1f}%, "
                      f"hits={r['hits']}/{r['total_rounds']}, "
                      f"netto={r['netto']:+,.0f} kr, "
                      f"avg cost={r['avg_cost_per_round']:,.0f}/round")

            best = positive_strategies[0]
            print(f"\n  RECOMMENDED STRATEGY:")
            print(f"    Strategy:     {best['strategy']}")
            print(f"    Description:  Pick top-{best['base_picks']} on confident races, "
                  f"ALL starters on {best['n_oppen']} most uncertain races")
            print(f"    Selector:     {best['selector']}")
            print(f"    Hits:         {best['hits']}/{best['total_rounds']} ({best['hit_rate']:.1%})")
            print(f"    Avg cost:     {best['avg_cost_per_round']:,.0f} kr/round")
            print(f"    Avg payout:   {best['avg_payout_per_hit']:,.0f} kr/hit")
            print(f"    Total netto:  {best['netto']:+,.0f} kr over {best['total_rounds']} rounds")
            print(f"    ROI:          {best['roi']*100:+.1f}%")
        else:
            print(f"\n  NO POSITIVE ROI STRATEGIES FOUND with current model accuracy.")

            # What would be needed?
            best = roi_results[0]
            print(f"\n  Best strategy: {best['strategy']}")
            print(f"    Hits: {best['hits']}/{best['total_rounds']} ({best['hit_rate']:.1%})")
            print(f"    ROI: {best['roi']*100:+.1f}%")
            print(f"    Avg cost: {best['avg_cost_per_round']:,.0f} kr/round")

            # Break-even analysis
            if best["total_cost"] > 0:
                needed_payout = best["total_cost"]
                if best["hits"] > 0:
                    needed_per_hit = needed_payout / best["hits"]
                    print(f"\n  To break even:")
                    print(f"    Need avg payout per hit: {needed_per_hit:,.0f} kr "
                          f"(currently {best['avg_payout_per_hit']:,.0f})")

                    # Or: how many more hits needed?
                    avg_payout_per_hit = best["avg_payout_per_hit"] if best["hits"] > 0 else 0
                    if avg_payout_per_hit > 0:
                        needed_hits = math.ceil(best["total_cost"] / avg_payout_per_hit)
                        needed_hit_rate = needed_hits / best["total_rounds"]
                        print(f"    OR need {needed_hits} hits ({needed_hit_rate:.1%} hit rate), "
                              f"currently {best['hits']} ({best['hit_rate']:.1%})")
                else:
                    print(f"\n  Zero hits — model needs fundamental improvement to reach profitability")

    # Clean roi_results for serialization (remove hit_details to save space)
    clean_roi_results = []
    for r in roi_results:
        cr = {k: v for k, v in r.items() if k != "hit_details"}
        clean_roi_results.append(cr)

    return {
        "roi_results": clean_roi_results,
        "selective_results": selective_results if 'selective_results' in dir() else {},
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    start_time = time.time()

    print("=" * 100)
    print("  BREAKTHROUGH ANALYSIS — V75/V85 Profitability Deep Dive")
    print("  Combining model (composite_score) with market (streckprocent)")
    print("  Testing adaptive picks, banker+oppen, dynamic selection")
    print("=" * 100)

    # Load data
    logger.info("Loading all cached V75+V85 rounds...")
    all_rounds = await load_rounds()
    logger.info(f"Loaded {len(all_rounds)} rounds in {time.time()-start_time:.0f}s")

    # Analyze
    logger.info("Analyzing all rounds with CompositeAnalyzer...")
    round_infos = analyze_all_rounds(all_rounds)

    v75 = [r for r in round_infos if r.game_type == "V75"]
    v85 = [r for r in round_infos if r.game_type == "V85"]
    print(f"\n  Rounds: {len(round_infos)} total ({len(v75)} V75, {len(v85)} V85)")
    dates = [r.round_date for r in round_infos]
    if dates:
        print(f"  Period: {min(dates)} to {max(dates)}")

    # ══ STEP 1 ══
    step1_results = step1_super_score(round_infos)
    best_alpha = step1_results["best_alpha"]

    # ══ STEP 2 ══
    step2_results = step2_adaptive_picks(round_infos, best_alpha)

    # ══ STEP 3 ══
    step3_results = step3_hittability(round_infos, best_alpha)
    round_analysis = step3_results["round_analysis"]

    # ══ STEP 4 ══
    step4_results = step4_breaker_analysis(round_infos, round_analysis, best_alpha)

    # ══ STEP 5 ══
    step5_results = step5_banker_oppen(round_infos, best_alpha)

    # ══ STEP 6 ══
    step6_results = step6_dynamic_oppen(round_infos, best_alpha)

    # ══ STEP 7 ══
    step7_results = step7_roi(round_infos, round_analysis, best_alpha, step6_results)

    # ── Save everything ──
    output = {
        "metadata": {
            "num_rounds": len(round_infos),
            "v75_rounds": len(v75),
            "v85_rounds": len(v85),
            "period_start": min(dates).isoformat() if dates else "",
            "period_end": max(dates).isoformat() if dates else "",
            "total_races": sum(len(r.races) for r in round_infos),
            "elapsed_seconds": round(time.time() - start_time, 1),
            "best_alpha": best_alpha,
        },
        "step1_super_score": {
            k: v for k, v in step1_results.items() if k != "alpha_results"
        },
        "step1_alpha_results": step1_results["alpha_results"],
        "step2_adaptive_picks": {
            str(k): {kk: vv for kk, vv in v.items() if kk != "details"}
            for k, v in step2_results.items()
        },
        "step3_summary": {
            "n_all_top2": step3_results["n_all_top2"],
            "n_all_top3": step3_results["n_all_top3"],
            "n_total": step3_results["n_total"],
        },
        "step4_breakers": {
            "n_breakers": len(step4_results.get("breakers_t3", [])),
            "breaker_summary": step4_results.get("breakers_t3", [])[:20],
        },
        "step5_banker_oppen": {
            k: v for k, v in step5_results.items()
        },
        "step6_dynamic_oppen": {
            k: {kk: vv for kk, vv in v.items()}
            for k, v in step6_results.items()
        },
        "step7_roi": step7_results,
        "per_round_analysis": [
            {
                "date": ra["date"],
                "game_type": ra["game_type"],
                "track": ra["track"],
                "all_in_top2": ra["all_in_top2"],
                "all_in_top3": ra["all_in_top3"],
                "worst_rank": ra["worst_rank"],
                "estimated_payout": round(ra["estimated_payout"], 0),
                "races": [
                    {
                        "race_number": r["race_number"],
                        "winner_pp": r["winner_pp"],
                        "winner_name": r["winner_name"],
                        "winner_super_rank": r["winner_super_rank"],
                        "winner_streck": round(r["winner_streck"], 4),
                        "num_starters": r["num_starters"],
                        "start_method": r["start_method"],
                        "confidence": round(r["confidence"], 1),
                        "upset_risk": round(r["upset_risk"], 1),
                    }
                    for r in ra["races"]
                ],
            }
            for ra in round_analysis
        ],
    }

    output_path = Path(__file__).parent / "breakthrough_analysis.json"
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    logger.info(f"Results saved to {output_path}")

    elapsed = time.time() - start_time
    print(f"\n  Total analysis time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    logger.info(f"Done in {elapsed:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
