#!/usr/bin/env python3
"""Deep ROI Analysis — Find patterns that predict hittable rounds.

PART 1: Actual payout data from ATG (turnover, jackpot, estimated payouts)
PART 2: Per-race hit analysis (model accuracy per race within each round)
PART 3: Pattern discovery (what distinguishes hittable from unhittable rounds)
PART 4: Optimal strategy search (grid search over hundreds of strategy variants)
PART 5: Estimated payouts and ROI calculation
PART 6: "Sniper" strategy (selective play based on EV estimation)

Run: python3 deep_roi_analysis.py
"""

from __future__ import annotations

import asyncio
import itertools
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
from trav_agent.data.models import GameRound, Race, RaceEntry

import logging

logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Constants ────────────────────────────────────────────────────────────────

ROW_PRICE = {"V75": 0.50, "V85": 1.00}

# ATG takes ~35% of pool for prize, rest is costs/margin
PRIZE_POOL_SHARE = 0.35

# Typical base pools when turnover is unavailable
DEFAULT_POOL = {"V75": 30_000_000, "V85": 5_000_000}


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class RaceDetail:
    """Detailed per-race analysis within a round."""
    race_number: int
    track_name: str
    num_starters: int
    start_method: str  # "auto" or "volt"

    # Winner info
    winner_pp: int
    winner_name: str
    winner_model_rank: int
    winner_streck: float
    winner_composite_score: float
    winner_recommendation: str  # what did we classify the winner as?

    # Model performance
    top1_hit: bool  # model #1 == winner
    top2_hit: bool  # winner in model top-2
    top3_hit: bool  # winner in model top-3

    # Race characteristics
    gap_to_second: float  # score gap rank1 - rank2
    gap_to_third: float
    top1_score: float  # model #1's composite score
    top1_streck: float  # model #1's streckprocent
    upset_risk: float
    recommendation_of_race: str  # "spik"/"2-val" etc for the top-ranked horse

    # All entries (for strategy simulation)
    sorted_entries: list[RaceEntry] = field(default_factory=list)
    upset_candidates: list[int] = field(default_factory=list)


@dataclass
class RoundDetail:
    """Complete round analysis with per-race details."""
    game_type: str
    game_id: str
    round_date: date
    track_name: str
    turnover: Optional[int]
    jackpot: Optional[int]

    races: list[RaceDetail] = field(default_factory=list)

    # Aggregate metrics (computed after races populated)
    num_top1_hits: int = 0
    num_top2_hits: int = 0
    num_top3_hits: int = 0
    worst_miss_rank: int = 0  # worst winner rank across races
    avg_winner_rank: float = 0.0
    avg_winner_streck: float = 0.0
    num_spikar: int = 0
    num_2val: int = 0
    num_strykning: int = 0
    avg_upset_risk: float = 0.0
    num_high_upset: int = 0
    avg_gap_to_second: float = 0.0
    avg_top1_score: float = 0.0
    avg_num_starters: float = 0.0
    all_in_top2: bool = False  # ALL winners in model top-2
    all_in_top3: bool = False  # ALL winners in model top-3

    # Payout estimation
    streck_product: float = 0.0
    estimated_payout_per_row: float = 0.0
    difficulty_score: float = 0.0  # 1/product(winner_strecks)


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


# ── Data Loading ─────────────────────────────────────────────────────────────

async def load_rounds() -> list[GameRound]:
    """Load all cached V75+V85 rounds (same as fine_tune_weights.py)."""
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


def analyze_all_rounds(all_rounds: list[GameRound]) -> list[RoundDetail]:
    """Analyze each round with CompositeAnalyzer and build RoundDetail."""
    round_details = []
    skipped = 0

    for idx, gr in enumerate(all_rounds):
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)

        analyzer = CompositeAnalyzer(AnalysisConfig())
        analyzer.analyze_round(gr_copy)

        expected_races = 7 if gr.game_type == "V75" else 8

        # Check completeness
        valid = True
        for race in gr_copy.races:
            if not race.result_order or not race.active_entries:
                valid = False
                break
        if not valid or len(gr_copy.races) < expected_races:
            skipped += 1
            continue

        rd = RoundDetail(
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
            ranking_pps = [e.post_position for e in sorted_entries]

            # Find winner entry
            winner_entry = None
            winner_rank = len(ranking_pps) + 1
            for i, e in enumerate(sorted_entries):
                if e.post_position == winner_pp:
                    winner_entry = e
                    winner_rank = i + 1
                    break

            if winner_entry is None:
                valid = False
                break

            gap_to_second = 0.0
            gap_to_third = 0.0
            if len(sorted_entries) >= 2:
                gap_to_second = sorted_entries[0].composite_score - sorted_entries[1].composite_score
            if len(sorted_entries) >= 3:
                gap_to_third = sorted_entries[0].composite_score - sorted_entries[2].composite_score

            top1_entry = sorted_entries[0]

            race_detail = RaceDetail(
                race_number=race.race_number,
                track_name=race.track_name,
                num_starters=race.num_starters,
                start_method=race.start_method.value,
                winner_pp=winner_pp,
                winner_name=winner_entry.horse.name,
                winner_model_rank=winner_rank,
                winner_streck=winner_entry.bet_percentage or 0.0,
                winner_composite_score=winner_entry.composite_score,
                winner_recommendation=winner_entry.recommendation,
                top1_hit=(winner_rank == 1),
                top2_hit=(winner_rank <= 2),
                top3_hit=(winner_rank <= 3),
                gap_to_second=gap_to_second,
                gap_to_third=gap_to_third,
                top1_score=top1_entry.composite_score,
                top1_streck=top1_entry.bet_percentage or 0.0,
                upset_risk=race.upset_risk,
                recommendation_of_race=top1_entry.recommendation,
                sorted_entries=sorted_entries,
                upset_candidates=race.upset_candidates,
            )
            rd.races.append(race_detail)

        if not valid or len(rd.races) < expected_races:
            skipped += 1
            continue

        # Compute aggregate metrics
        rd.num_top1_hits = sum(1 for r in rd.races if r.top1_hit)
        rd.num_top2_hits = sum(1 for r in rd.races if r.top2_hit)
        rd.num_top3_hits = sum(1 for r in rd.races if r.top3_hit)
        rd.worst_miss_rank = max(r.winner_model_rank for r in rd.races)
        rd.avg_winner_rank = statistics.mean([r.winner_model_rank for r in rd.races])
        winner_strecks = [r.winner_streck for r in rd.races if r.winner_streck > 0]
        rd.avg_winner_streck = statistics.mean(winner_strecks) if winner_strecks else 0.0
        rd.num_spikar = sum(1 for r in rd.races if r.recommendation_of_race == "spik")
        rd.num_2val = sum(1 for r in rd.races if r.recommendation_of_race == "2-val")
        rd.num_strykning = sum(
            1 for r in rd.races
            if any(e.recommendation == "strykning" for e in r.sorted_entries[:3])
        )
        rd.avg_upset_risk = statistics.mean([r.upset_risk for r in rd.races])
        rd.num_high_upset = sum(1 for r in rd.races if r.upset_risk > 50)
        rd.avg_gap_to_second = statistics.mean([r.gap_to_second for r in rd.races])
        rd.avg_top1_score = statistics.mean([r.top1_score for r in rd.races])
        rd.avg_num_starters = statistics.mean([r.num_starters for r in rd.races])
        rd.all_in_top2 = all(r.top2_hit for r in rd.races)
        rd.all_in_top3 = all(r.top3_hit for r in rd.races)

        # Payout estimation
        ws = [max(r.winner_streck, 0.001) for r in rd.races]
        rd.streck_product = product(ws)
        pool = rd.turnover if rd.turnover else DEFAULT_POOL.get(rd.game_type, 10_000_000)
        prize_pool = pool * PRIZE_POOL_SHARE
        row_price = ROW_PRICE.get(rd.game_type, 1.0)
        pool_rows = pool / row_price
        expected_winners = pool_rows * rd.streck_product
        if expected_winners < 0.001:
            expected_winners = 0.001
        rd.estimated_payout_per_row = min(prize_pool, prize_pool / expected_winners)
        rd.difficulty_score = 1.0 / rd.streck_product if rd.streck_product > 0 else 999999

        round_details.append(rd)

        if (idx + 1) % 20 == 0:
            logger.info(f"  Analyzed {idx+1}/{len(all_rounds)}...")

    logger.info(f"Total valid rounds: {len(round_details)} (skipped {skipped})")
    return round_details


# ══════════════════════════════════════════════════════════════════════════════
# PART 1: ACTUAL PAYOUTS
# ══════════════════════════════════════════════════════════════════════════════

def part1_payouts(round_details: list[RoundDetail]):
    sep("PART 1: PAYOUT DATA FROM ATG")

    with_turnover = [r for r in round_details if r.turnover and r.turnover > 0]
    with_jackpot = [r for r in round_details if r.jackpot and r.jackpot > 0]

    print(f"\n  Rounds with turnover data: {len(with_turnover)}/{len(round_details)}")
    print(f"  Rounds with jackpot data:  {len(with_jackpot)}/{len(round_details)}")

    if with_turnover:
        v75_to = [r.turnover for r in with_turnover if r.game_type == "V75"]
        v85_to = [r.turnover for r in with_turnover if r.game_type == "V85"]
        if v75_to:
            print(f"\n  V75 Turnover:")
            print(f"    Avg: {statistics.mean(v75_to):,.0f} kr")
            print(f"    Min: {min(v75_to):,.0f} kr  |  Max: {max(v75_to):,.0f} kr")
            print(f"    Prize pool (35%): {statistics.mean(v75_to) * 0.35:,.0f} kr avg")
        if v85_to:
            print(f"\n  V85 Turnover:")
            print(f"    Avg: {statistics.mean(v85_to):,.0f} kr")
            print(f"    Min: {min(v85_to):,.0f} kr  |  Max: {max(v85_to):,.0f} kr")
            print(f"    Prize pool (35%): {statistics.mean(v85_to) * 0.35:,.0f} kr avg")

    if with_jackpot:
        v75_jp = [r.jackpot for r in with_jackpot if r.game_type == "V75"]
        v85_jp = [r.jackpot for r in with_jackpot if r.game_type == "V85"]
        if v75_jp:
            print(f"\n  V75 Jackpots: {len(v75_jp)} rounds")
            print(f"    Avg: {statistics.mean(v75_jp):,.0f} kr")
            print(f"    Max: {max(v75_jp):,.0f} kr")
        if v85_jp:
            print(f"\n  V85 Jackpots: {len(v85_jp)} rounds")
            print(f"    Avg: {statistics.mean(v85_jp):,.0f} kr")
            print(f"    Max: {max(v85_jp):,.0f} kr")

    # Estimated payouts distribution
    print(f"\n  Estimated Payout Per Row (all rounds):")
    payouts = [r.estimated_payout_per_row for r in round_details]
    payouts_sorted = sorted(payouts, reverse=True)
    print(f"    Mean:   {statistics.mean(payouts):,.0f} kr")
    print(f"    Median: {statistics.median(payouts):,.0f} kr")
    print(f"    Max:    {max(payouts):,.0f} kr")
    print(f"    Min:    {min(payouts):,.0f} kr")
    print(f"    Top 10: {', '.join(f'{p:,.0f}' for p in payouts_sorted[:10])}")

    # V75 vs V85 payout comparison
    v75_payouts = [r.estimated_payout_per_row for r in round_details if r.game_type == "V75"]
    v85_payouts = [r.estimated_payout_per_row for r in round_details if r.game_type == "V85"]
    if v75_payouts:
        print(f"\n  V75 Est. Payout: mean={statistics.mean(v75_payouts):,.0f}, median={statistics.median(v75_payouts):,.0f}")
    if v85_payouts:
        print(f"  V85 Est. Payout: mean={statistics.mean(v85_payouts):,.0f}, median={statistics.median(v85_payouts):,.0f}")


# ══════════════════════════════════════════════════════════════════════════════
# PART 2: PER-RACE HIT ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def part2_per_race_analysis(round_details: list[RoundDetail]):
    sep("PART 2: PER-RACE HIT ANALYSIS")

    total_races = sum(len(r.races) for r in round_details)
    total_top1 = sum(r.num_top1_hits for r in round_details)
    total_top2 = sum(r.num_top2_hits for r in round_details)
    total_top3 = sum(r.num_top3_hits for r in round_details)

    print(f"\n  Total rounds: {len(round_details)}")
    print(f"  Total races:  {total_races}")
    print(f"\n  Overall race-level accuracy:")
    print(f"    Top-1 hit rate: {total_top1}/{total_races} = {total_top1/total_races:.1%}")
    print(f"    Top-2 hit rate: {total_top2}/{total_races} = {total_top2/total_races:.1%}")
    print(f"    Top-3 hit rate: {total_top3}/{total_races} = {total_top3/total_races:.1%}")

    # Distribution of winner model rank
    all_winner_ranks = []
    for rd in round_details:
        for race in rd.races:
            all_winner_ranks.append(race.winner_model_rank)
    rank_dist = defaultdict(int)
    for r in all_winner_ranks:
        rank_dist[r] += 1

    print(f"\n  Winner's model rank distribution:")
    for rank in sorted(rank_dist.keys()):
        count = rank_dist[rank]
        pct = count / len(all_winner_ranks) * 100
        bar = "#" * int(pct)
        print(f"    Rank {rank:>2}: {count:>4} ({pct:>5.1f}%) {bar}")

    # Per-round top-N hit counts
    print(f"\n  Per-round hit distribution:")
    print(f"  {'Metric':<40} {'Count':>6} {'%':>8}")
    print(f"  {'-'*40} {'-'*6} {'-'*8}")

    n_rounds = len(round_details)
    all_top2 = sum(1 for r in round_details if r.all_in_top2)
    all_top3 = sum(1 for r in round_details if r.all_in_top3)

    print(f"  {'ALL winners in top-2 (perfect 2-pick sys)':<40} {all_top2:>6} {all_top2/n_rounds*100:>7.1f}%")
    print(f"  {'ALL winners in top-3 (perfect 3-pick sys)':<40} {all_top3:>6} {all_top3/n_rounds*100:>7.1f}%")

    for game_type in ["V75", "V85"]:
        gt_rounds = [r for r in round_details if r.game_type == game_type]
        if not gt_rounds:
            continue
        n_races = 7 if game_type == "V75" else 8

        # Near-miss analysis
        for miss in range(0, 3):
            threshold = n_races - miss
            count = sum(1 for r in gt_rounds if r.num_top2_hits >= threshold)
            label = f"{game_type}: >= {threshold}/{n_races} in top-2"
            print(f"  {label:<40} {count:>6} {count/len(gt_rounds)*100:>7.1f}%")

        for miss in range(0, 3):
            threshold = n_races - miss
            count = sum(1 for r in gt_rounds if r.num_top3_hits >= threshold)
            label = f"{game_type}: >= {threshold}/{n_races} in top-3"
            print(f"  {label:<40} {count:>6} {count/len(gt_rounds)*100:>7.1f}%")

    # Winner's streck distribution
    print(f"\n  Winner streck distribution:")
    streck_buckets = [
        ("Favorites (>25%)", 0.25, 1.0),
        ("Mid-favorites (15-25%)", 0.15, 0.25),
        ("Contenders (10-15%)", 0.10, 0.15),
        ("Longshots (5-10%)", 0.05, 0.10),
        ("Big upsets (<5%)", 0.0, 0.05),
    ]
    all_winner_strecks = [race.winner_streck for rd in round_details for race in rd.races if race.winner_streck > 0]
    for label, lo, hi in streck_buckets:
        count = sum(1 for s in all_winner_strecks if lo <= s < hi)
        pct = count / len(all_winner_strecks) * 100 if all_winner_strecks else 0
        print(f"    {label:<30} {count:>5} ({pct:>5.1f}%)")

    # Recommendation vs actual winner
    print(f"\n  What was the WINNER classified as by the model?")
    winner_recs = defaultdict(int)
    for rd in round_details:
        for race in rd.races:
            winner_recs[race.winner_recommendation] += 1
    for rec in ["spik", "2-val", "3-val", "gardering", "strykning"]:
        count = winner_recs.get(rec, 0)
        pct = count / total_races * 100
        print(f"    {rec:<15} {count:>5} ({pct:>5.1f}%)")


# ══════════════════════════════════════════════════════════════════════════════
# PART 3: PATTERN DISCOVERY
# ══════════════════════════════════════════════════════════════════════════════

def part3_pattern_discovery(round_details: list[RoundDetail]):
    sep("PART 3: PATTERN DISCOVERY — What makes a round hittable?")

    all_top2_rounds = [r for r in round_details if r.all_in_top2]
    all_top3_rounds = [r for r in round_details if r.all_in_top3]
    unhittable = [r for r in round_details if not r.all_in_top3]

    def describe_group(label, group):
        if not group:
            print(f"\n  {label}: 0 rounds — NONE")
            return
        print(f"\n  {label}: {len(group)} rounds")
        print(f"    Avg upset_risk:        {statistics.mean([r.avg_upset_risk for r in group]):.1f}")
        print(f"    Avg num_spikar:        {statistics.mean([r.num_spikar for r in group]):.1f}")
        print(f"    Avg num_2val:          {statistics.mean([r.num_2val for r in group]):.1f}")
        print(f"    Avg winner_streck:     {statistics.mean([r.avg_winner_streck for r in group]):.1%}")
        print(f"    Avg winner_rank:       {statistics.mean([r.avg_winner_rank for r in group]):.2f}")
        print(f"    Avg gap_to_second:     {statistics.mean([r.avg_gap_to_second for r in group]):.1f}")
        print(f"    Avg top1_score:        {statistics.mean([r.avg_top1_score for r in group]):.1f}")
        print(f"    Avg num_starters:      {statistics.mean([r.avg_num_starters for r in group]):.1f}")
        print(f"    Avg num_high_upset:    {statistics.mean([r.num_high_upset for r in group]):.1f}")
        print(f"    Avg est payout/row:    {statistics.mean([r.estimated_payout_per_row for r in group]):,.0f} kr")
        gt_counts = defaultdict(int)
        for r in group:
            gt_counts[r.game_type] += 1
        print(f"    Game types:            {dict(gt_counts)}")
        dates = [r.round_date.isoformat() for r in group]
        if len(dates) <= 10:
            print(f"    Dates:                 {', '.join(dates)}")
        else:
            print(f"    Date range:            {min(dates)} to {max(dates)}")

    describe_group("PERFECT TOP-2 ROUNDS (all winners in model top-2)", all_top2_rounds)
    describe_group("PERFECT TOP-3 ROUNDS (all winners in model top-3)", all_top3_rounds)
    describe_group("UNHITTABLE ROUNDS (at least 1 winner outside top-3)", unhittable)

    # Statistical comparison
    print(f"\n  STATISTICAL COMPARISON (hittable top-3 vs unhittable):")
    if all_top3_rounds and unhittable:
        metrics = [
            ("avg_upset_risk", "Avg Upset Risk"),
            ("avg_winner_streck", "Avg Winner Streck"),
            ("avg_gap_to_second", "Avg Gap to #2"),
            ("avg_top1_score", "Avg Top1 Score"),
            ("avg_num_starters", "Avg Starters"),
            ("num_spikar", "Num Spikar"),
            ("num_high_upset", "Num High Upset"),
        ]
        print(f"    {'Metric':<25} {'Hittable (top3)':>18} {'Unhittable':>18} {'Diff':>10}")
        print(f"    {'-'*25} {'-'*18} {'-'*18} {'-'*10}")
        for attr, label in metrics:
            hit_val = statistics.mean([getattr(r, attr) for r in all_top3_rounds])
            miss_val = statistics.mean([getattr(r, attr) for r in unhittable])
            diff = hit_val - miss_val
            print(f"    {label:<25} {hit_val:>18.2f} {miss_val:>18.2f} {diff:>+10.2f}")

    # Near-miss analysis: rounds where all-but-1 in top-2
    sep("NEAR-MISS ANALYSIS: Rounds where ONE race broke the system", "-")
    for gt in ["V75", "V85"]:
        gt_rounds = [r for r in round_details if r.game_type == gt]
        n_races = 7 if gt == "V75" else 8
        near_misses = [r for r in gt_rounds if r.num_top2_hits == n_races - 1]

        print(f"\n  {gt}: {len(near_misses)} near-miss rounds ({n_races-1}/{n_races} in top-2)")

        if near_misses:
            breaking_races = []
            for rd in near_misses:
                for race in rd.races:
                    if not race.top2_hit:
                        breaking_races.append(race)

            if breaking_races:
                avg_rank = statistics.mean([r.winner_model_rank for r in breaking_races])
                avg_streck = statistics.mean([r.winner_streck for r in breaking_races if r.winner_streck > 0])
                avg_upset = statistics.mean([r.upset_risk for r in breaking_races])
                avg_starters = statistics.mean([r.num_starters for r in breaking_races])

                print(f"    The ONE breaking race characteristics:")
                print(f"      Avg winner model rank:  {avg_rank:.1f}")
                print(f"      Avg winner streck:      {avg_streck:.1%}")
                print(f"      Avg upset_risk:         {avg_upset:.1f}")
                print(f"      Avg num_starters:       {avg_starters:.1f}")

                # What recommendations did the winners of breaking races have?
                rec_counts = defaultdict(int)
                for r in breaking_races:
                    rec_counts[r.winner_recommendation] += 1
                print(f"      Winner recommendations: {dict(rec_counts)}")

                # Race number distribution (are certain positions more error-prone?)
                race_num_counts = defaultdict(int)
                for r in breaking_races:
                    race_num_counts[r.race_number] += 1
                print(f"      Race number dist:       {dict(sorted(race_num_counts.items()))}")


# ══════════════════════════════════════════════════════════════════════════════
# PART 4: OPTIMAL STRATEGY SEARCH
# ══════════════════════════════════════════════════════════════════════════════

def simulate_strategy(rd: RoundDetail, params: dict) -> dict:
    """Simulate a strategy on a round. Returns picks, cost, hit, payout."""
    game_type = rd.game_type
    row_price = ROW_PRICE.get(game_type, 1.0)
    max_budget = params.get("max_budget", 1000)
    max_rows = int(max_budget / row_price)

    spike_gap = params.get("spike_gap", 10)
    spike_picks = params.get("spike_picks", 1)
    confident_gap = params.get("confident_gap", 5)
    confident_picks = params.get("confident_picks", 2)
    normal_picks = params.get("normal_picks", 3)
    upset_threshold = params.get("upset_threshold", 40)
    upset_picks = params.get("upset_picks", 5)

    picks_per_race = []
    for race in rd.races:
        sorted_pps = [e.post_position for e in race.sorted_entries]
        gap = race.gap_to_second
        risk = race.upset_risk
        n_starters = race.num_starters

        if gap >= spike_gap and risk < upset_threshold:
            n = min(spike_picks, n_starters)
        elif gap >= confident_gap and risk < upset_threshold:
            n = min(confident_picks, n_starters)
        elif risk >= upset_threshold:
            n = min(upset_picks, n_starters)
        else:
            n = min(normal_picks, n_starters)

        picks_per_race.append(sorted_pps[:n])

    # Constrain to budget
    total_rows = product(len(p) for p in picks_per_race)
    while total_rows > max_rows and any(len(p) > 1 for p in picks_per_race):
        # Find race with most picks that has highest upset risk (least confident)
        best_idx = -1
        best_score = -1
        for i, p in enumerate(picks_per_race):
            if len(p) <= 1:
                continue
            score = rd.races[i].upset_risk + len(p) * 5 - rd.races[i].gap_to_second
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx < 0:
            break
        picks_per_race[best_idx] = picks_per_race[best_idx][:-1]
        total_rows = product(len(p) for p in picks_per_race)

    # Try to expand if under budget
    while total_rows < max_rows * 0.8:
        expanded = False
        for i in range(len(picks_per_race)):
            race = rd.races[i]
            curr_len = len(picks_per_race[i])
            if curr_len >= race.num_starters:
                continue
            new_rows = total_rows * (curr_len + 1) / curr_len
            if new_rows <= max_rows:
                sorted_pps = [e.post_position for e in race.sorted_entries]
                if curr_len < len(sorted_pps):
                    picks_per_race[i] = sorted_pps[:curr_len + 1]
                    total_rows = product(len(p) for p in picks_per_race)
                    expanded = True
                    break
        if not expanded:
            break

    total_rows = product(len(p) for p in picks_per_race)
    cost = total_rows * row_price

    # Check hit
    hit = True
    for i, race in enumerate(rd.races):
        if race.winner_pp not in picks_per_race[i]:
            hit = False
            break

    payout = rd.estimated_payout_per_row if hit else 0.0

    return {
        "picks_per_race": [len(p) for p in picks_per_race],
        "total_rows": total_rows,
        "cost": cost,
        "hit": hit,
        "payout": payout,
    }


def part4_strategy_search(round_details: list[RoundDetail]) -> dict:
    sep("PART 4: OPTIMAL STRATEGY SEARCH (Grid Search)")

    # Define parameter grid
    param_grid = {
        "spike_gap": [8, 10, 12, 15],
        "spike_picks": [1, 2],
        "confident_gap": [3, 5, 7],
        "confident_picks": [1, 2, 3],
        "normal_picks": [2, 3, 4],
        "upset_threshold": [20, 30, 40, 50],
        "upset_picks": [3, 4, 5, 6],
        "max_budget": [200, 500, 1000, 2000, 5000],
    }

    # Generate all combinations
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    all_combos = list(itertools.product(*values))
    logger.info(f"Testing {len(all_combos)} strategy combinations...")

    best_by_budget = {}  # budget -> (roi, params, hits, cost, payout)
    all_results = []  # for selective filtering

    for combo_idx, combo in enumerate(all_combos):
        params = dict(zip(keys, combo))
        budget = params["max_budget"]

        total_cost = 0.0
        total_payout = 0.0
        hits = 0
        n = 0

        for rd in round_details:
            result = simulate_strategy(rd, params)
            total_cost += result["cost"]
            total_payout += result["payout"]
            if result["hit"]:
                hits += 1
            n += 1

        roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0

        if budget not in best_by_budget or roi > best_by_budget[budget][0]:
            best_by_budget[budget] = (roi, params, hits, total_cost, total_payout)

        all_results.append({
            "params": params,
            "roi": roi,
            "hits": hits,
            "total_cost": total_cost,
            "total_payout": total_payout,
            "n_rounds": n,
        })

        if (combo_idx + 1) % 5000 == 0:
            logger.info(f"  Tested {combo_idx+1}/{len(all_combos)}...")

    # Sort all results by ROI
    all_results.sort(key=lambda x: -x["roi"])

    print(f"\n  Tested {len(all_combos)} strategy combinations across {len(round_details)} rounds")

    print(f"\n  BEST STRATEGY PER BUDGET:")
    print(f"  {'Budget':>8} {'ROI':>8} {'Hits':>6} {'Cost':>12} {'Payout':>12} {'Netto':>12}  Params")
    print(f"  {'-'*8} {'-'*8} {'-'*6} {'-'*12} {'-'*12} {'-'*12}  {'-'*40}")

    for budget in sorted(best_by_budget.keys()):
        roi, params, hits, cost, payout = best_by_budget[budget]
        netto = payout - cost
        p_str = f"sg={params['spike_gap']},sp={params['spike_picks']},cg={params['confident_gap']},cp={params['confident_picks']},np={params['normal_picks']},ut={params['upset_threshold']},up={params['upset_picks']}"
        print(f"  {budget:>8} {roi*100:>+7.1f}% {hits:>6} {cost:>12,.0f} {payout:>12,.0f} {netto:>+12,.0f}  {p_str}")

    # Top 20 overall
    print(f"\n  TOP 20 STRATEGIES (by ROI):")
    print(f"  {'#':>3} {'Budget':>7} {'ROI':>8} {'Hits':>5}/{len(round_details):>2} {'Netto':>12}  Key Params")
    print(f"  {'-'*3} {'-'*7} {'-'*8} {'-'*8} {'-'*12}  {'-'*50}")
    for i, r in enumerate(all_results[:20]):
        p = r["params"]
        netto = r["total_payout"] - r["total_cost"]
        print(f"  {i+1:>3} {p['max_budget']:>7} {r['roi']*100:>+7.1f}% {r['hits']:>5}/{r['n_rounds']:>2} {netto:>+12,.0f}  sg={p['spike_gap']},sp={p['spike_picks']},cg={p['confident_gap']},cp={p['confident_picks']},np={p['normal_picks']},ut={p['upset_threshold']},up={p['upset_picks']}")

    # ── SELECTIVE PLAY ──
    sep("PART 4B: SELECTIVE PLAY — Only play certain rounds", "-")

    selective_filters = {
        "skip_2plus_high_upset": lambda rd: rd.num_high_upset < 2,
        "at_least_1_spik": lambda rd: rd.num_spikar >= 1,
        "at_least_2_spikar": lambda rd: rd.num_spikar >= 2,
        "avg_winner_streck_gt_15": lambda rd: rd.avg_winner_streck > 0.15,
        "avg_gap_gt_5": lambda rd: rd.avg_gap_to_second > 5,
        "avg_gap_gt_8": lambda rd: rd.avg_gap_to_second > 8,
        "avg_upset_risk_lt_30": lambda rd: rd.avg_upset_risk < 30,
        "avg_upset_risk_lt_40": lambda rd: rd.avg_upset_risk < 40,
        "no_high_upset": lambda rd: rd.num_high_upset == 0,
        "top1_score_gt_75": lambda rd: rd.avg_top1_score > 75,
        "max_10_starters": lambda rd: rd.avg_num_starters <= 10,
    }

    # Use best overall params (from top ROI entry)
    best_overall_params = all_results[0]["params"] if all_results else {}

    selective_results = {}
    print(f"\n  Using best params: {best_overall_params}")
    print(f"\n  {'Filter':<35} {'Rounds':>7} {'Hits':>5} {'Cost':>10} {'Payout':>12} {'ROI':>8} {'Hit%':>7}")
    print(f"  {'-'*35} {'-'*7} {'-'*5} {'-'*10} {'-'*12} {'-'*8} {'-'*7}")

    # Also all rounds baseline
    for filt_name, filt_fn in [("ALL_ROUNDS", lambda rd: True)] + list(selective_filters.items()):
        filtered = [rd for rd in round_details if filt_fn(rd)]
        if not filtered:
            print(f"  {filt_name:<35} {0:>7} {'':>5} {'':>10} {'':>12} {'':>8} {'':>7}")
            continue

        total_cost = 0.0
        total_payout = 0.0
        hits = 0
        for rd in filtered:
            result = simulate_strategy(rd, best_overall_params)
            total_cost += result["cost"]
            total_payout += result["payout"]
            if result["hit"]:
                hits += 1

        roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
        hit_pct = hits / len(filtered) * 100
        print(f"  {filt_name:<35} {len(filtered):>7} {hits:>5} {total_cost:>10,.0f} {total_payout:>12,.0f} {roi*100:>+7.1f}% {hit_pct:>6.1f}%")

        selective_results[filt_name] = {
            "n_rounds": len(filtered),
            "hits": hits,
            "cost": total_cost,
            "payout": total_payout,
            "roi": roi,
        }

    # Test multiple budgets with selective play
    sep("PART 4C: BUDGET x SELECTIVE MATRIX", "-")

    # Find best selective filter per budget
    budgets_to_test = [200, 500, 1000, 2000, 5000]
    filter_list = [("ALL", lambda rd: True)] + [(k, v) for k, v in selective_filters.items()]

    print(f"\n  {'Filter':<35}", end="")
    for b in budgets_to_test:
        print(f"  {b:>7} kr", end="")
    print()
    print(f"  {'-'*35}", end="")
    for _ in budgets_to_test:
        print(f"  {'-'*9}", end="")
    print()

    matrix_results = {}
    for filt_name, filt_fn in filter_list:
        filtered = [rd for rd in round_details if filt_fn(rd)]
        if not filtered:
            continue

        print(f"  {filt_name:<35}", end="")
        matrix_results[filt_name] = {}

        for budget in budgets_to_test:
            params = dict(best_overall_params)
            params["max_budget"] = budget

            total_cost = 0.0
            total_payout = 0.0
            hits = 0
            for rd in filtered:
                result = simulate_strategy(rd, params)
                total_cost += result["cost"]
                total_payout += result["payout"]
                if result["hit"]:
                    hits += 1

            roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
            print(f"  {roi*100:>+7.0f}%", end="")
            matrix_results[filt_name][budget] = {
                "roi": roi, "hits": hits, "n": len(filtered),
                "cost": total_cost, "payout": total_payout,
            }

        print()

    return {
        "best_by_budget": {
            str(b): {
                "roi": v[0], "params": v[1], "hits": v[2],
                "cost": v[3], "payout": v[4],
            }
            for b, v in best_by_budget.items()
        },
        "top20": all_results[:20],
        "selective_results": selective_results,
        "matrix_results": {
            f: {str(b): v for b, v in bv.items()}
            for f, bv in matrix_results.items()
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# PART 5: ESTIMATED PAYOUTS (THE KEY QUESTION)
# ══════════════════════════════════════════════════════════════════════════════

def part5_payout_analysis(round_details: list[RoundDetail]):
    sep("PART 5: ESTIMATED PAYOUTS — Would hits pay enough?")

    # Categorize rounds by difficulty
    easy = [r for r in round_details if r.difficulty_score < 100]
    medium = [r for r in round_details if 100 <= r.difficulty_score < 10000]
    hard = [r for r in round_details if 10000 <= r.difficulty_score < 1000000]
    extreme = [r for r in round_details if r.difficulty_score >= 1000000]

    print(f"\n  Round difficulty distribution:")
    print(f"    Easy (diff < 100):         {len(easy):>4} rounds")
    print(f"    Medium (100 - 10k):        {len(medium):>4} rounds")
    print(f"    Hard (10k - 1M):           {len(hard):>4} rounds")
    print(f"    Extreme (>1M):             {len(extreme):>4} rounds")

    # For each difficulty tier, show avg payout and model accuracy
    for label, group in [("Easy", easy), ("Medium", medium), ("Hard", hard), ("Extreme", extreme)]:
        if not group:
            continue
        avg_payout = statistics.mean([r.estimated_payout_per_row for r in group])
        avg_rank = statistics.mean([r.avg_winner_rank for r in group])
        all_t2 = sum(1 for r in group if r.all_in_top2) / len(group) * 100
        all_t3 = sum(1 for r in group if r.all_in_top3) / len(group) * 100
        print(f"\n  {label}:")
        print(f"    Avg est payout/row: {avg_payout:>12,.0f} kr")
        print(f"    Avg winner rank:    {avg_rank:>12.2f}")
        print(f"    % all-in-top-2:     {all_t2:>12.1f}%")
        print(f"    % all-in-top-3:     {all_t3:>12.1f}%")

    # The key question: for rounds we CAN hit, what do they pay?
    sep("KEY ANALYSIS: Hittable rounds vs their payouts", "-")

    for n_picks, label in [(2, "2-pick system"), (3, "3-pick system")]:
        hittable = [r for r in round_details if all(
            race.winner_model_rank <= n_picks for race in r.races
        )]
        if not hittable:
            print(f"\n  {label}: 0 hittable rounds")
            continue

        payouts = [r.estimated_payout_per_row for r in hittable]
        n_races_per = 7 if hittable[0].game_type == "V75" else 8  # approximate
        row_price_approx = 0.75  # average of V75 0.50 and V85 1.00
        # Cost for N picks per race
        system_rows = n_picks ** statistics.mean([len(r.races) for r in hittable])
        system_cost = system_rows * row_price_approx

        total_payout = sum(payouts)
        total_rounds = len(round_details)
        total_cost = system_cost * total_rounds

        print(f"\n  {label.upper()}:")
        print(f"    Hittable rounds:    {len(hittable)}/{len(round_details)} ({len(hittable)/len(round_details)*100:.1f}%)")
        print(f"    Avg payout/row:     {statistics.mean(payouts):,.0f} kr")
        print(f"    Max payout:         {max(payouts):,.0f} kr")
        print(f"    Min payout:         {min(payouts):,.0f} kr")
        print(f"    Approx rows/round:  {system_rows:,.0f}")
        print(f"    Approx cost/round:  {system_cost:,.0f} kr")
        print(f"    Total payout (hits):{total_payout:,.0f} kr")
        print(f"    Total cost (all rds):{total_cost:,.0f} kr")
        roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
        print(f"    ROI:                {roi*100:+.1f}%")

    # Per-round analysis of hittable rounds
    print(f"\n  INDIVIDUAL HITTABLE ROUNDS (all winners in top-3):")
    top3_hittable = [r for r in round_details if r.all_in_top3]
    if top3_hittable:
        top3_hittable.sort(key=lambda r: r.estimated_payout_per_row, reverse=True)
        print(f"  {'Date':<12} {'Type':<5} {'Payout':>12} {'AvgRank':>8} {'AvgStreck':>10} {'AvgUpset':>9} {'Spikar':>7}")
        print(f"  {'-'*12} {'-'*5} {'-'*12} {'-'*8} {'-'*10} {'-'*9} {'-'*7}")
        for r in top3_hittable:
            print(f"  {r.round_date.isoformat():<12} {r.game_type:<5} {r.estimated_payout_per_row:>12,.0f} {r.avg_winner_rank:>8.2f} {r.avg_winner_streck:>9.1%} {r.avg_upset_risk:>9.1f} {r.num_spikar:>7}")


# ══════════════════════════════════════════════════════════════════════════════
# PART 6: SNIPER STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

def part6_sniper_strategy(round_details: list[RoundDetail]):
    sep("PART 6: SNIPER STRATEGY — Selective EV-positive play")

    # Estimate P(hit) from round characteristics
    # We'll build a simple model: P(hit) based on avg_gap, num_spikar, avg_upset_risk

    # First, let's see what actual hit rates are for different round profiles
    # Using a 3-pick-per-race system as baseline

    # Group rounds by characteristics and compute hit rate
    print(f"\n  Step 1: Empirical P(hit | round features) for 3-pick system")

    for attr_name, attr_label, thresholds in [
        ("avg_upset_risk", "Avg Upset Risk", [20, 30, 40, 50]),
        ("avg_gap_to_second", "Avg Gap to #2", [3, 5, 8, 10]),
        ("num_spikar", "Num Spikar", [0, 1, 2, 3]),
        ("num_high_upset", "Num High Upset", [0, 1, 2]),
        ("avg_winner_streck", "Avg Winner Streck", [0.10, 0.15, 0.20]),
        ("avg_top1_score", "Avg Top1 Score", [65, 70, 75, 80]),
    ]:
        print(f"\n  {attr_label}:")
        bins = []
        prev = None
        for t in thresholds + [999]:
            if prev is None:
                lo = -999
            else:
                lo = prev
            hi = t
            rounds_in_bin = [r for r in round_details if lo <= getattr(r, attr_name) < hi]
            if rounds_in_bin:
                hit_3 = sum(1 for r in rounds_in_bin if r.all_in_top3)
                hit_2 = sum(1 for r in rounds_in_bin if r.all_in_top2)
                avg_payout = statistics.mean([r.estimated_payout_per_row for r in rounds_in_bin])
                if lo == -999:
                    label = f"< {hi}"
                elif hi == 999:
                    label = f">= {lo}"
                else:
                    label = f"{lo} - {hi}"
                print(f"    {label:>15}: {len(rounds_in_bin):>3} rounds, "
                      f"top3_hit={hit_3/len(rounds_in_bin)*100:>5.1f}%, "
                      f"top2_hit={hit_2/len(rounds_in_bin)*100:>5.1f}%, "
                      f"avg_payout={avg_payout:>10,.0f}")
            prev = t

    # Step 2: Build simple EV estimator
    sep("Step 2: EV-based Sniper Strategy", "-")

    # For each budget, test sniper approach:
    # Only play if estimated EV > 0
    # EV = P(hit) * payout - cost
    # P(hit) estimated from training data (simple lookup)

    # Build P(hit) lookup from all data (leave-one-out would be better but slow)
    # Use combined features: avg_gap > X AND num_high_upset < Y
    print(f"\n  Testing Sniper configurations:")
    print(f"  A round is played only if model_confidence_score > threshold")
    print(f"  model_confidence = avg_gap_to_second * (1 + num_spikar) / (1 + num_high_upset)")

    # Compute confidence score for each round
    for rd in round_details:
        rd._conf_score = (rd.avg_gap_to_second * (1 + rd.num_spikar) / (1 + rd.num_high_upset))

    conf_scores = [rd._conf_score for rd in round_details]
    print(f"\n  Confidence score distribution:")
    print(f"    Mean: {statistics.mean(conf_scores):.1f}")
    print(f"    Median: {statistics.median(conf_scores):.1f}")
    print(f"    Min: {min(conf_scores):.1f}  Max: {max(conf_scores):.1f}")

    # Test multiple thresholds
    budgets = [200, 500, 1000, 2000, 5000]
    thresholds = sorted(set([
        round(statistics.quantiles(conf_scores, n=10)[i], 1)
        for i in range(9)
    ] + [0, 5, 8, 10, 12, 15, 20]))

    print(f"\n  {'Threshold':>10} {'Played':>7} {'%':>6}", end="")
    for b in budgets:
        print(f" | {b:>6}kr ROI", end="")
    print()
    print(f"  {'-'*10} {'-'*7} {'-'*6}", end="")
    for _ in budgets:
        print(f" | {'-'*12}", end="")
    print()

    sniper_results = {}
    best_sniper = {"roi": -999, "budget": 0, "threshold": 0, "details": {}}

    for threshold in thresholds:
        played = [rd for rd in round_details if rd._conf_score >= threshold]
        if len(played) < 3:
            continue

        played_pct = len(played) / len(round_details) * 100
        print(f"  {threshold:>10.1f} {len(played):>7} {played_pct:>5.1f}%", end="")

        for budget in budgets:
            # Use best params from grid search but with this budget
            params = {
                "spike_gap": 10, "spike_picks": 1,
                "confident_gap": 5, "confident_picks": 2,
                "normal_picks": 3, "upset_threshold": 40,
                "upset_picks": 5, "max_budget": budget,
            }

            total_cost = 0.0
            total_payout = 0.0
            hits = 0
            for rd in played:
                result = simulate_strategy(rd, params)
                total_cost += result["cost"]
                total_payout += result["payout"]
                if result["hit"]:
                    hits += 1

            roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
            print(f" | {roi*100:>+7.0f}% h={hits}", end="")

            if roi > best_sniper["roi"]:
                best_sniper = {
                    "roi": roi,
                    "budget": budget,
                    "threshold": threshold,
                    "details": {
                        "played": len(played),
                        "hits": hits,
                        "cost": total_cost,
                        "payout": total_payout,
                    }
                }

            sniper_results[f"t{threshold}_b{budget}"] = {
                "threshold": threshold,
                "budget": budget,
                "played": len(played),
                "hits": hits,
                "cost": total_cost,
                "payout": total_payout,
                "roi": roi,
            }

        print()

    # ── Advanced Sniper: multi-signal ──
    sep("Step 3: Advanced Multi-Signal Sniper", "-")

    # Combine multiple signals
    print(f"\n  Testing combined filters for optimal EV:")
    combo_filters = [
        ("conf>=10 + upset<2", lambda rd: rd._conf_score >= 10 and rd.num_high_upset < 2),
        ("conf>=8 + upset<2", lambda rd: rd._conf_score >= 8 and rd.num_high_upset < 2),
        ("conf>=8 + upset<1", lambda rd: rd._conf_score >= 8 and rd.num_high_upset < 1),
        ("gap>5 + spikar>=1", lambda rd: rd.avg_gap_to_second > 5 and rd.num_spikar >= 1),
        ("gap>5 + spikar>=2", lambda rd: rd.avg_gap_to_second > 5 and rd.num_spikar >= 2),
        ("gap>8 + upset<30", lambda rd: rd.avg_gap_to_second > 8 and rd.avg_upset_risk < 30),
        ("gap>5 + upset_risk<30", lambda rd: rd.avg_gap_to_second > 5 and rd.avg_upset_risk < 30),
        ("gap>5 + upset_risk<40", lambda rd: rd.avg_gap_to_second > 5 and rd.avg_upset_risk < 40),
        ("top1>75 + upset<2", lambda rd: rd.avg_top1_score > 75 and rd.num_high_upset < 2),
        ("top1>70 + gap>5", lambda rd: rd.avg_top1_score > 70 and rd.avg_gap_to_second > 5),
        ("top1>75 + gap>5 + upset<2", lambda rd: rd.avg_top1_score > 75 and rd.avg_gap_to_second > 5 and rd.num_high_upset < 2),
        ("spikar>=1 + upset<2 + gap>5", lambda rd: rd.num_spikar >= 1 and rd.num_high_upset < 2 and rd.avg_gap_to_second > 5),
        ("spikar>=2 + upset<1", lambda rd: rd.num_spikar >= 2 and rd.num_high_upset < 1),
        ("V75_only", lambda rd: rd.game_type == "V75"),
        ("V85_only", lambda rd: rd.game_type == "V85"),
    ]

    print(f"  {'Filter':<40} {'N':>4}", end="")
    for b in budgets:
        print(f"  {b:>7}kr", end="")
    print()
    print(f"  {'-'*40} {'-'*4}", end="")
    for _ in budgets:
        print(f"  {'-'*9}", end="")
    print()

    best_combo_sniper = {"filter": "", "roi": -999, "budget": 0}
    advanced_results = {}

    for filt_name, filt_fn in combo_filters:
        filtered = [rd for rd in round_details if filt_fn(rd)]
        if len(filtered) < 2:
            continue

        print(f"  {filt_name:<40} {len(filtered):>4}", end="")
        advanced_results[filt_name] = {}

        for budget in budgets:
            params = {
                "spike_gap": 10, "spike_picks": 1,
                "confident_gap": 5, "confident_picks": 2,
                "normal_picks": 3, "upset_threshold": 40,
                "upset_picks": 5, "max_budget": budget,
            }

            total_cost = 0.0
            total_payout = 0.0
            hits = 0
            for rd in filtered:
                result = simulate_strategy(rd, params)
                total_cost += result["cost"]
                total_payout += result["payout"]
                if result["hit"]:
                    hits += 1

            roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
            print(f"  {roi*100:>+6.0f}% ({hits})", end="")

            if roi > best_combo_sniper["roi"]:
                best_combo_sniper = {
                    "filter": filt_name, "roi": roi, "budget": budget,
                    "hits": hits, "played": len(filtered),
                    "cost": total_cost, "payout": total_payout,
                }
            advanced_results[filt_name][str(budget)] = {
                "roi": roi, "hits": hits, "n": len(filtered),
                "cost": total_cost, "payout": total_payout,
            }

        print()

    # Final recommendation
    sep("FINAL SUMMARY", "=")

    print(f"\n  BEST SNIPER CONFIGURATION:")
    bs = best_sniper
    print(f"    Threshold:    conf_score >= {bs['threshold']}")
    print(f"    Budget:       {bs['budget']} kr/round")
    d = bs["details"]
    print(f"    Rounds played:{d.get('played', 0)}/{len(round_details)}")
    print(f"    Hits:         {d.get('hits', 0)}")
    print(f"    Total cost:   {d.get('cost', 0):,.0f} kr")
    print(f"    Total payout: {d.get('payout', 0):,.0f} kr")
    print(f"    ROI:          {bs['roi']*100:+.1f}%")
    print(f"    Netto:        {d.get('payout', 0) - d.get('cost', 0):+,.0f} kr")

    print(f"\n  BEST ADVANCED SNIPER:")
    bc = best_combo_sniper
    print(f"    Filter:       {bc.get('filter', 'N/A')}")
    print(f"    Budget:       {bc.get('budget', 0)} kr/round")
    print(f"    Rounds:       {bc.get('played', 0)}/{len(round_details)}")
    print(f"    Hits:         {bc.get('hits', 0)}")
    print(f"    ROI:          {bc.get('roi', 0)*100:+.1f}%")
    if bc.get('cost', 0) > 0:
        print(f"    Netto:        {bc.get('payout', 0) - bc.get('cost', 0):+,.0f} kr")

    # ── The verdict ──
    sep("THE VERDICT: Can we achieve positive ROI?", "=")

    # Check: any strategy/filter/budget combo that's positive?
    positive_combos = []
    for key, val in sniper_results.items():
        if val["roi"] > 0 and val["played"] >= 5:
            positive_combos.append(val)
    for filt, bdict in advanced_results.items():
        for budget_str, val in bdict.items():
            if val["roi"] > 0 and val["n"] >= 5:
                positive_combos.append({**val, "filter": filt, "budget": int(budget_str)})

    if positive_combos:
        positive_combos.sort(key=lambda x: -x["roi"])
        print(f"\n  YES — {len(positive_combos)} positive-ROI configurations found!")
        print(f"\n  Top 10:")
        for i, c in enumerate(positive_combos[:10]):
            filt = c.get("filter", f"conf>={c.get('threshold','?')}")
            b = c.get("budget", "?")
            print(f"    {i+1}. [{filt}] @ {b}kr: "
                  f"ROI={c['roi']*100:+.1f}%, "
                  f"{c.get('hits',0)} hits/{c.get('played', c.get('n',0))} played, "
                  f"netto={c.get('payout',0)-c.get('cost',0):+,.0f} kr")

        best = positive_combos[0]
        print(f"\n  RECOMMENDED STRATEGY:")
        threshold_val = best.get('threshold', '?')
        filter_desc = best.get('filter', f'conf >= {threshold_val}')
        print(f"    Use filter: {filter_desc}")
        print(f"    Budget: {best.get('budget', '?')} kr per round")
        print(f"    Expected ROI: {best['roi']*100:+.1f}%")
        print(f"    Play {best.get('played', best.get('n', '?'))} of {len(round_details)} rounds ({best.get('played', best.get('n', 0))/len(round_details)*100:.0f}%)")
    else:
        print(f"\n  NO — No strategy achieves reliably positive ROI with >= 5 rounds")
        print(f"\n  WHAT NEEDS TO IMPROVE:")

        # Calculate required accuracy
        avg_payout = statistics.mean([r.estimated_payout_per_row for r in round_details])
        for budget in [500, 1000, 2000]:
            row_price = 0.75  # avg
            rows = budget / row_price
            needed_hit_rate = budget / avg_payout
            print(f"\n    Budget {budget} kr:")
            print(f"      Avg est payout: {avg_payout:,.0f} kr")
            print(f"      Required hit rate for break-even: {needed_hit_rate*100:.2f}%")
            print(f"      Current all-in-top-3 rate: {sum(1 for r in round_details if r.all_in_top3)/len(round_details)*100:.1f}%")

        # What top-N accuracy is needed?
        print(f"\n    MODEL ACCURACY IMPROVEMENT NEEDED:")
        current_top3 = sum(sum(1 for race in rd.races if race.top3_hit) for rd in round_details) / sum(len(rd.races) for rd in round_details)
        print(f"      Current per-race top-3: {current_top3:.1%}")
        for target in [0.60, 0.65, 0.70, 0.75, 0.80]:
            # If per-race top-3 = target, what's P(all hit) for 7 races?
            p_all_7 = target ** 7
            p_all_8 = target ** 8
            print(f"      If top-3 = {target:.0%}: P(all V75) = {p_all_7:.1%}, P(all V85) = {p_all_8:.1%}")

    return {
        "best_sniper": best_sniper,
        "best_combo_sniper": best_combo_sniper,
        "sniper_results": sniper_results,
        "advanced_results": advanced_results,
        "positive_combos": len(positive_combos) if 'positive_combos' in dir() else 0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    start_time = time.time()

    print("=" * 100)
    print("  DEEP ROI ANALYSIS — Finding patterns for positive-ROI betting")
    print("  V75 + V85 historical rounds, CompositeAnalyzer model")
    print("=" * 100)

    # 1. Load data
    logger.info("Loading all cached V75+V85 rounds...")
    all_rounds = await load_rounds()
    logger.info(f"Loaded {len(all_rounds)} rounds in {time.time()-start_time:.0f}s")

    # 2. Analyze each round
    logger.info("Analyzing all rounds with CompositeAnalyzer...")
    round_details = analyze_all_rounds(all_rounds)

    print(f"\n  Rounds analyzed: {len(round_details)}")
    v75 = [r for r in round_details if r.game_type == "V75"]
    v85 = [r for r in round_details if r.game_type == "V85"]
    print(f"    V75: {len(v75)}  |  V85: {len(v85)}")
    dates = [r.round_date for r in round_details]
    if dates:
        print(f"    Period: {min(dates)} to {max(dates)}")

    # ── PART 1 ──
    part1_payouts(round_details)

    # ── PART 2 ──
    part2_per_race_analysis(round_details)

    # ── PART 3 ──
    part3_pattern_discovery(round_details)

    # ── PART 4 ──
    strategy_results = part4_strategy_search(round_details)

    # ── PART 5 ──
    part5_payout_analysis(round_details)

    # ── PART 6 ──
    sniper_results = part6_sniper_strategy(round_details)

    # ── Save JSON ──
    output = {
        "metadata": {
            "num_rounds": len(round_details),
            "v75_rounds": len(v75),
            "v85_rounds": len(v85),
            "period_start": min(dates).isoformat() if dates else "",
            "period_end": max(dates).isoformat() if dates else "",
            "total_races": sum(len(r.races) for r in round_details),
            "elapsed_seconds": round(time.time() - start_time, 1),
        },
        "per_round_summary": [
            {
                "date": rd.round_date.isoformat(),
                "game_type": rd.game_type,
                "game_id": rd.game_id,
                "track": rd.track_name,
                "turnover": rd.turnover,
                "jackpot": rd.jackpot,
                "num_top1_hits": rd.num_top1_hits,
                "num_top2_hits": rd.num_top2_hits,
                "num_top3_hits": rd.num_top3_hits,
                "worst_miss_rank": rd.worst_miss_rank,
                "avg_winner_rank": round(rd.avg_winner_rank, 3),
                "avg_winner_streck": round(rd.avg_winner_streck, 4),
                "num_spikar": rd.num_spikar,
                "avg_upset_risk": round(rd.avg_upset_risk, 1),
                "num_high_upset": rd.num_high_upset,
                "avg_gap_to_second": round(rd.avg_gap_to_second, 2),
                "avg_top1_score": round(rd.avg_top1_score, 1),
                "avg_num_starters": round(rd.avg_num_starters, 1),
                "all_in_top2": rd.all_in_top2,
                "all_in_top3": rd.all_in_top3,
                "estimated_payout_per_row": round(rd.estimated_payout_per_row, 0),
                "difficulty_score": round(rd.difficulty_score, 1),
                "races": [
                    {
                        "race_number": race.race_number,
                        "winner_pp": race.winner_pp,
                        "winner_name": race.winner_name,
                        "winner_model_rank": race.winner_model_rank,
                        "winner_streck": round(race.winner_streck, 4),
                        "top1_hit": race.top1_hit,
                        "top2_hit": race.top2_hit,
                        "top3_hit": race.top3_hit,
                        "gap_to_second": round(race.gap_to_second, 2),
                        "upset_risk": round(race.upset_risk, 1),
                        "num_starters": race.num_starters,
                        "recommendation": race.recommendation_of_race,
                        "winner_recommendation": race.winner_recommendation,
                    }
                    for race in rd.races
                ],
            }
            for rd in round_details
        ],
        "strategy_search": {
            k: v for k, v in strategy_results.items()
            if k != "top20"  # top20 contains non-serializable data
        },
        "strategy_top20": [
            {
                "params": r["params"],
                "roi": round(r["roi"], 4),
                "hits": r["hits"],
                "total_cost": round(r["total_cost"], 2),
                "total_payout": round(r["total_payout"], 2),
            }
            for r in strategy_results.get("top20", [])
        ],
        "sniper": {
            "best_simple": sniper_results.get("best_sniper", {}),
            "best_advanced": sniper_results.get("best_combo_sniper", {}),
        },
    }

    output_path = Path(__file__).parent / "deep_roi_analysis.json"
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    logger.info(f"Results saved to {output_path}")

    elapsed = time.time() - start_time
    print(f"\n  Total analysis time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    logger.info(f"Done in {elapsed:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
