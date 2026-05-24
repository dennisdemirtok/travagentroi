#!/usr/bin/env python3
"""ROI Backtest Simulator for V75/V85 Horse Racing Model.

Simulates actual betting strategies on historical rounds:
- Loads cached V75+V85 rounds
- Filters future starts (no data leakage)
- Analyzes each round with CompositeAnalyzer
- For each round, builds system tickets per strategy
- Evaluates: did we hit all races? If yes, estimate payout.
- Tracks cost, payout, ROI across all rounds and strategies.

Strategies:
  1. Snal (Tight) - minimize rows
  2. Balanserad (Balanced) - standard approach
  3. Brett (Wide) - maximize coverage
  4. Skrall (Upset-focused) - based on upset_risk
  5. Value (Value-based) - score/streck ratio

Run: python3 roi_backtest.py
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from functools import reduce
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

# Typical Swedish pool sizes (kr)
POOL_TOTAL = {"V75": 30_000_000, "V85": 5_000_000}
PRIZE_POOL_SHARE = 0.35  # 35% of pool goes to top prize (7-ratt / 8-ratt)

# Total rows in the public pool
POOL_TOTAL_ROWS = {
    "V75": 30_000_000 / 0.50,   # 60M rows
    "V85": 5_000_000 / 1.00,    # 5M rows
}

BUDGETS = [50, 100, 200, 500, 1000, 2000, 5000]

STRATEGY_NAMES = ["snal", "balanserad", "brett", "skrall", "value"]


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class RaceAnalysis:
    """Per-race analysis data needed for strategy decisions."""
    race_number: int
    recommendation_map: dict[str, list[int]]  # recommendation -> [horse numbers]
    sorted_entries: list[RaceEntry]
    winner: int
    winner_streck: float
    upset_risk: float
    upset_candidates: list[int]
    num_starters: int
    gap_to_second: float


@dataclass
class RoundAnalysis:
    """Complete round analysis."""
    game_type: str
    round_date: date
    races: list[RaceAnalysis]
    turnover: Optional[int] = None


@dataclass
class StrategyResult:
    """Result of applying a strategy to a single round."""
    picks_per_race: list[list[int]]  # horse numbers picked per race
    total_rows: int
    cost: float
    hit: bool  # did all races have winner in picks?
    estimated_payout: float
    winner_strecks: list[float]  # streck of winner in each race


@dataclass
class BacktestSummary:
    """Aggregate results for a strategy x budget combo."""
    strategy: str
    budget: int
    num_rounds: int = 0
    num_hits: int = 0
    total_cost: float = 0.0
    total_payout: float = 0.0
    payouts: list[float] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)
    hit_rounds: list[str] = field(default_factory=list)  # dates of hits
    round_details: list[dict] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return self.num_hits / max(1, self.num_rounds)

    @property
    def roi(self) -> float:
        if self.total_cost == 0:
            return 0.0
        return (self.total_payout - self.total_cost) / self.total_cost

    @property
    def best_payout(self) -> float:
        return max(self.payouts) if self.payouts else 0.0

    @property
    def worst_streak(self) -> int:
        """Longest streak without a hit."""
        if not self.round_details:
            return 0
        max_streak = 0
        current = 0
        for rd in self.round_details:
            if rd.get("hit"):
                current = 0
            else:
                current += 1
                max_streak = max(max_streak, current)
        return max_streak


# ── Helper functions ─────────────────────────────────────────────────────────

def filter_future_starts(game_round: GameRound) -> None:
    """Remove starts that occurred after the race date (prevent data leakage)."""
    for race in game_round.races:
        for entry in race.entries:
            entry.horse.past_starts = [
                s for s in entry.horse.past_starts if s.start_date < race.race_date
            ]


def product(iterable):
    """Product of all elements."""
    result = 1
    for x in iterable:
        result *= x
    return result


def estimate_payout(
    game_type: str,
    winner_strecks: list[float],
    turnover: Optional[int] = None,
) -> float:
    """Estimate payout per winning row.

    payout_per_row = prize_pool / estimated_winning_rows
    estimated_winning_rows = pool_total_rows * product(winner_streck_i)

    Args:
        game_type: "V75" or "V85"
        winner_strecks: bet_percentage of the winner in each race
        turnover: actual turnover if available
    """
    pool = turnover if turnover else POOL_TOTAL.get(game_type, 10_000_000)
    prize_pool = pool * PRIZE_POOL_SHARE
    pool_rows = POOL_TOTAL_ROWS.get(game_type, 10_000_000)

    if turnover:
        row_price = ROW_PRICE.get(game_type, 1.0)
        pool_rows = turnover / row_price

    # Product of winner strecks = probability that a random row hits all races
    streck_product = product(max(0.001, s) for s in winner_strecks)

    # Expected number of winning rows in the pool
    expected_winners = pool_rows * streck_product

    if expected_winners < 0.001:
        expected_winners = 0.001

    payout_per_row = prize_pool / expected_winners

    # Cap at realistic maximum (jackpots can go higher but let's be conservative)
    max_payout = prize_pool * 0.90  # At most 90% of prize pool to one winner
    payout_per_row = min(payout_per_row, max_payout)

    return payout_per_row


def constrain_rows(
    picks_per_race: list[list[int]],
    max_rows: int,
    race_analyses: list[RaceAnalysis],
) -> list[list[int]]:
    """Reduce picks in least confident races to fit within budget.

    Strategy: iteratively remove the last pick from the race with the
    highest upset_risk or the smallest gap (least confident).
    """
    current = [list(p) for p in picks_per_race]

    while product(len(p) for p in current) > max_rows:
        # Find the race with the most picks that we can reduce
        # Prefer reducing races with highest upset_risk
        best_idx = -1
        best_score = -1

        for i, (picks, ra) in enumerate(zip(current, race_analyses)):
            if len(picks) <= 1:
                continue
            # Score: higher = more reducible
            # High upset_risk + many picks = good candidate to reduce
            score = ra.upset_risk + len(picks) * 10 - ra.gap_to_second
            if score > best_score:
                best_score = score
                best_idx = i

        if best_idx < 0:
            break  # Can't reduce further

        # Remove the last (worst) pick from that race
        current[best_idx] = current[best_idx][:-1]

    return current


def expand_rows_to_budget(
    picks_per_race: list[list[int]],
    max_rows: int,
    race_analyses: list[RaceAnalysis],
) -> list[list[int]]:
    """If we have budget left, add more picks to least confident races."""
    current = [list(p) for p in picks_per_race]

    while product(len(p) for p in current) < max_rows:
        # Find race where adding one more pick has the best ROI
        best_idx = -1
        best_new_rows = float("inf")

        for i, (picks, ra) in enumerate(zip(current, race_analyses)):
            if len(picks) >= ra.num_starters:
                continue
            # How many new rows would adding one pick create?
            current_rows = product(len(p) for p in current)
            # Adding one pick to race i multiplies rows by (len+1)/len
            new_total = current_rows * (len(picks) + 1) / len(picks)
            if new_total <= max_rows and ra.upset_risk > best_new_rows * 0.5:
                best_new_rows = new_total
                best_idx = i

        if best_idx < 0:
            break

        # Add the next ranked horse
        ra = race_analyses[best_idx]
        current_nums = set(current[best_idx])
        for entry in ra.sorted_entries:
            if entry.post_position not in current_nums:
                current[best_idx].append(entry.post_position)
                break
        else:
            break  # No more horses to add

        # Check if we're still within budget
        if product(len(p) for p in current) > max_rows:
            current[best_idx] = current[best_idx][:-1]
            break

    return current


# ── Strategy implementations ─────────────────────────────────────────────────

def strategy_snal(race_analyses: list[RaceAnalysis]) -> list[list[int]]:
    """Snal (Tight): minimize rows.

    - spik: pick 1 (the spike)
    - 2-val: pick top 2
    - 3-val/gardering: pick top 2
    - strykning: pick top 2
    """
    picks = []
    for ra in race_analyses:
        recs = ra.recommendation_map
        sorted_nums = [e.post_position for e in ra.sorted_entries]

        spik_horses = recs.get("spik", [])
        if spik_horses:
            # Spike race: just pick the spike
            picks.append(spik_horses[:1])
        else:
            # Pick top 2 regardless of recommendation
            picks.append(sorted_nums[:2])

    return picks


def strategy_balanserad(race_analyses: list[RaceAnalysis]) -> list[list[int]]:
    """Balanserad (Balanced): standard approach.

    - spik: pick 1
    - 2-val: pick 2
    - 3-val: pick 3
    - gardering: pick 3
    - strykning: pick top 2
    """
    picks = []
    for ra in race_analyses:
        recs = ra.recommendation_map
        sorted_nums = [e.post_position for e in ra.sorted_entries]

        spik_horses = recs.get("spik", [])
        tva_val = recs.get("2-val", [])
        tre_val = recs.get("3-val", [])
        gardering = recs.get("gardering", [])

        if spik_horses:
            picks.append(spik_horses[:1])
        elif len(tva_val) >= 2:
            picks.append(sorted_nums[:2])
        elif tre_val or gardering:
            picks.append(sorted_nums[:3])
        else:
            picks.append(sorted_nums[:2])

    return picks


def strategy_brett(race_analyses: list[RaceAnalysis]) -> list[list[int]]:
    """Brett (Wide): maximize coverage.

    - spik: pick 2
    - 2-val: pick 3
    - 3-val: pick 4
    - gardering: pick 4
    - strykning: pick 3
    """
    picks = []
    for ra in race_analyses:
        recs = ra.recommendation_map
        sorted_nums = [e.post_position for e in ra.sorted_entries]

        spik_horses = recs.get("spik", [])
        tva_val = recs.get("2-val", [])
        tre_val = recs.get("3-val", [])
        gardering = recs.get("gardering", [])

        if spik_horses:
            picks.append(sorted_nums[:2])
        elif len(tva_val) >= 2:
            picks.append(sorted_nums[:3])
        elif tre_val or gardering:
            picks.append(sorted_nums[:4])
        else:
            picks.append(sorted_nums[:3])

    return picks


def strategy_skrall(race_analyses: list[RaceAnalysis]) -> list[list[int]]:
    """Skrall (Upset-focused): based on upset_risk.

    - Low upset_risk (<30): pick top 1-2
    - Medium upset_risk (30-50): pick top 4
    - High upset_risk (>50): pick top 5-6
    - Always include any upset_candidates in selections
    """
    picks = []
    for ra in race_analyses:
        sorted_nums = [e.post_position for e in ra.sorted_entries]
        risk = ra.upset_risk

        if risk < 30:
            base_picks = sorted_nums[:2]
        elif risk <= 50:
            base_picks = sorted_nums[:4]
        else:
            base_picks = sorted_nums[:6]

        # Always include upset candidates
        pick_set = set(base_picks)
        for uc in ra.upset_candidates:
            pick_set.add(uc)

        # Maintain order by model ranking
        ordered = [n for n in sorted_nums if n in pick_set]
        picks.append(ordered if ordered else sorted_nums[:1])

    return picks


def strategy_value(race_analyses: list[RaceAnalysis]) -> list[list[int]]:
    """Value (Value-based): select horses with good score/streck ratio.

    Select horses where:
    - model score >= 30 (gardering_min_score) AND either:
      - model rank <= 3, OR
      - bet_percentage < 0.10 (low streck = value), OR
      - value_index (score / (streck*100)) > 1.5
    Minimum 1 (top), maximum 5 per race.
    """
    picks = []
    for ra in race_analyses:
        sorted_nums = [e.post_position for e in ra.sorted_entries]
        value_picks = []

        for entry in ra.sorted_entries:
            score = entry.composite_score
            streck = entry.bet_percentage or 0.0

            if score < 30:
                continue

            is_top3 = entry.rank <= 3
            is_low_streck = streck > 0 and streck < 0.10
            value_idx = (score / (streck * 100)) if streck > 0 else score / 10
            is_value = value_idx > 1.5

            if is_top3 or is_low_streck or is_value:
                value_picks.append(entry.post_position)

            if len(value_picks) >= 5:
                break

        if not value_picks:
            value_picks = sorted_nums[:1]

        picks.append(value_picks)

    return picks


STRATEGIES = {
    "snal": strategy_snal,
    "balanserad": strategy_balanserad,
    "brett": strategy_brett,
    "skrall": strategy_skrall,
    "value": strategy_value,
}


# ── Analysis pipeline ────────────────────────────────────────────────────────

def analyze_round(game_round: GameRound) -> Optional[RoundAnalysis]:
    """Run CompositeAnalyzer and extract per-race analysis data."""
    gr = game_round.model_copy(deep=True)
    filter_future_starts(gr)

    analyzer = CompositeAnalyzer(AnalysisConfig())
    analyzer.analyze_round(gr)

    races = []
    for race in gr.races:
        if not race.result_order or not race.active_entries:
            return None  # Skip incomplete rounds

        winner = race.result_order[0]

        # Find winner's streck
        winner_streck = 0.0
        for entry in race.active_entries:
            if entry.post_position == winner:
                winner_streck = entry.bet_percentage or 0.0
                break

        # Build recommendation map
        rec_map: dict[str, list[int]] = defaultdict(list)
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.composite_score,
            reverse=True,
        )
        for entry in sorted_entries:
            rec_map[entry.recommendation].append(entry.post_position)

        # Gap to second
        gap = 0.0
        if len(sorted_entries) >= 2:
            gap = sorted_entries[0].composite_score - sorted_entries[1].composite_score

        races.append(RaceAnalysis(
            race_number=race.race_number,
            recommendation_map=dict(rec_map),
            sorted_entries=sorted_entries,
            winner=winner,
            winner_streck=winner_streck,
            upset_risk=race.upset_risk,
            upset_candidates=race.upset_candidates,
            num_starters=race.num_starters,
            gap_to_second=gap,
        ))

    expected_races = 7 if game_round.game_type == "V75" else 8
    if len(races) < expected_races:
        return None  # Skip rounds with missing races

    return RoundAnalysis(
        game_type=game_round.game_type,
        round_date=game_round.round_date,
        races=races,
        turnover=game_round.turnover,
    )


def evaluate_strategy(
    round_analysis: RoundAnalysis,
    strategy_name: str,
    budget: int,
) -> StrategyResult:
    """Apply a strategy to a round and evaluate the result."""
    game_type = round_analysis.game_type
    row_price = ROW_PRICE.get(game_type, 1.0)
    max_rows = int(budget / row_price)

    # Get initial picks from strategy
    strategy_fn = STRATEGIES[strategy_name]
    raw_picks = strategy_fn(round_analysis.races)

    # Constrain to budget
    picks = constrain_rows(raw_picks, max_rows, round_analysis.races)

    # Calculate rows and cost
    total_rows = product(len(p) for p in picks)
    cost = total_rows * row_price

    # Check if we hit all races
    hit = True
    winner_strecks = []
    for i, (race_picks, ra) in enumerate(zip(picks, round_analysis.races)):
        winner_strecks.append(ra.winner_streck)
        if ra.winner not in race_picks:
            hit = False

    # Estimate payout
    payout = 0.0
    if hit:
        payout_per_row = estimate_payout(
            game_type, winner_strecks, round_analysis.turnover
        )
        # We have exactly 1 winning combination in our system
        # (assuming our picks include the winner in each race,
        # there's exactly 1 combo that matches all winners)
        payout = payout_per_row

    return StrategyResult(
        picks_per_race=picks,
        total_rows=total_rows,
        cost=cost,
        hit=hit,
        estimated_payout=payout,
        winner_strecks=winner_strecks,
    )


# ── Selective betting filters ────────────────────────────────────────────────

def filter_high_confidence(round_analysis: RoundAnalysis) -> bool:
    """Only play rounds with at least 2 spikes."""
    spike_count = sum(
        1 for ra in round_analysis.races
        if ra.recommendation_map.get("spik")
    )
    return spike_count >= 2


def filter_low_upset(round_analysis: RoundAnalysis) -> bool:
    """Only play rounds where average upset_risk < 30."""
    avg_risk = sum(ra.upset_risk for ra in round_analysis.races) / len(round_analysis.races)
    return avg_risk < 30


def filter_moderate_upset(round_analysis: RoundAnalysis) -> bool:
    """Only play rounds where average upset_risk < 40."""
    avg_risk = sum(ra.upset_risk for ra in round_analysis.races) / len(round_analysis.races)
    return avg_risk < 40


def filter_at_least_1_spike(round_analysis: RoundAnalysis) -> bool:
    """Only play rounds with at least 1 spike."""
    spike_count = sum(
        1 for ra in round_analysis.races
        if ra.recommendation_map.get("spik")
    )
    return spike_count >= 1


SELECTIVE_FILTERS = {
    "alla_omgangar": lambda ra: True,
    "minst_2_spikar": filter_high_confidence,
    "minst_1_spik": filter_at_least_1_spike,
    "snitt_risk_under_30": filter_low_upset,
    "snitt_risk_under_40": filter_moderate_upset,
}


# ── Main backtest ────────────────────────────────────────────────────────────

async def load_rounds() -> list[GameRound]:
    """Load all cached V75+V85 rounds (same method as fine_tune_weights.py)."""
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


def run_backtest(
    round_analyses: list[RoundAnalysis],
) -> dict[str, dict[int, BacktestSummary]]:
    """Run all strategies x budgets on all rounds.

    Returns: {strategy_name: {budget: BacktestSummary}}
    """
    results: dict[str, dict[int, BacktestSummary]] = {}

    for strat in STRATEGY_NAMES:
        results[strat] = {}
        for budget in BUDGETS:
            results[strat][budget] = BacktestSummary(
                strategy=strat, budget=budget
            )

    for ra in round_analyses:
        for strat in STRATEGY_NAMES:
            for budget in BUDGETS:
                summary = results[strat][budget]
                sr = evaluate_strategy(ra, strat, budget)

                summary.num_rounds += 1
                summary.total_cost += sr.cost
                summary.costs.append(sr.cost)

                if sr.hit:
                    summary.num_hits += 1
                    summary.total_payout += sr.estimated_payout
                    summary.payouts.append(sr.estimated_payout)
                    summary.hit_rounds.append(ra.round_date.isoformat())

                summary.round_details.append({
                    "date": ra.round_date.isoformat(),
                    "game_type": ra.game_type,
                    "hit": sr.hit,
                    "cost": sr.cost,
                    "payout": sr.estimated_payout,
                    "rows": sr.total_rows,
                    "picks_per_race": [len(p) for p in sr.picks_per_race],
                })

    return results


def print_report(
    results: dict[str, dict[int, BacktestSummary]],
    round_analyses: list[RoundAnalysis],
    selective_results: dict[str, dict[str, dict[int, BacktestSummary]]],
):
    """Print comprehensive backtest report."""

    print("\n" + "=" * 100)
    print("  V75/V85 ROI BACKTEST SIMULATOR  --  RESULTAT")
    print("=" * 100)
    print(f"  Antal omgangar: {len(round_analyses)}")
    v75_count = sum(1 for ra in round_analyses if ra.game_type == "V75")
    v85_count = sum(1 for ra in round_analyses if ra.game_type == "V85")
    print(f"  V75: {v75_count} omgangar, V85: {v85_count} omgangar")
    if round_analyses:
        dates = [ra.round_date for ra in round_analyses]
        print(f"  Period: {min(dates)} till {max(dates)}")
    print()

    # ── 1. Strategy x Budget matrix ──────────────────────────────────────
    print("\n" + "=" * 100)
    print("  1. STRATEGI x BUDGET MATRIS (ROI %)")
    print("=" * 100)

    # Header
    header = f"{'Strategi':<15}"
    for b in BUDGETS:
        header += f"  {b:>6} kr"
    print(header)
    print("-" * len(header))

    best_roi = -999
    best_combo = ("", 0)

    for strat in STRATEGY_NAMES:
        line = f"{strat:<15}"
        for budget in BUDGETS:
            s = results[strat][budget]
            roi_pct = s.roi * 100
            if roi_pct > best_roi:
                best_roi = roi_pct
                best_combo = (strat, budget)
            line += f"  {roi_pct:>+6.0f}%"
        print(line)

    print()

    # Hit rate matrix
    print("\n  HIT RATE (% omgangar med 7/8 ratt)")
    print("-" * 80)
    header = f"{'Strategi':<15}"
    for b in BUDGETS:
        header += f"  {b:>6} kr"
    print(header)
    print("-" * len(header))

    for strat in STRATEGY_NAMES:
        line = f"{strat:<15}"
        for budget in BUDGETS:
            s = results[strat][budget]
            line += f"  {s.hit_rate*100:>6.1f}%"
        print(line)

    print()

    # Total hits matrix
    print("\n  ANTAL HITS")
    print("-" * 80)
    header = f"{'Strategi':<15}"
    for b in BUDGETS:
        header += f"  {b:>6} kr"
    print(header)
    print("-" * len(header))

    for strat in STRATEGY_NAMES:
        line = f"{strat:<15}"
        for budget in BUDGETS:
            s = results[strat][budget]
            line += f"  {s.num_hits:>6}"
        print(line)

    print()

    # Total payout matrix
    print("\n  TOTAL UTBETALNING (kr)")
    print("-" * 80)
    header = f"{'Strategi':<15}"
    for b in BUDGETS:
        header += f"  {b:>6} kr"
    print(header)
    print("-" * len(header))

    for strat in STRATEGY_NAMES:
        line = f"{strat:<15}"
        for budget in BUDGETS:
            s = results[strat][budget]
            line += f"  {s.total_payout:>8.0f}"
        print(line)

    print()

    # Total cost matrix
    print("\n  TOTAL KOSTNAD (kr)")
    print("-" * 80)
    header = f"{'Strategi':<15}"
    for b in BUDGETS:
        header += f"  {b:>6} kr"
    print(header)
    print("-" * len(header))

    for strat in STRATEGY_NAMES:
        line = f"{strat:<15}"
        for budget in BUDGETS:
            s = results[strat][budget]
            line += f"  {s.total_cost:>8.0f}"
        print(line)

    print()

    # ── 2. Best strategy overall ─────────────────────────────────────────
    print("\n" + "=" * 100)
    print("  2. BASTA STRATEGI OVERALL")
    print("=" * 100)
    print(f"  Basta ROI: {best_combo[0]} med budget {best_combo[1]} kr")
    best_s = results[best_combo[0]][best_combo[1]]
    print(f"    ROI:           {best_s.roi*100:+.1f}%")
    print(f"    Hit rate:      {best_s.hit_rate*100:.1f}%")
    print(f"    Antal hits:    {best_s.num_hits} av {best_s.num_rounds}")
    print(f"    Total kostnad: {best_s.total_cost:,.0f} kr")
    print(f"    Total utbet:   {best_s.total_payout:,.0f} kr")
    print(f"    Netto:         {best_s.total_payout - best_s.total_cost:,.0f} kr")
    print()

    # ── 3. Monthly breakdown for best strategy ───────────────────────────
    print("\n" + "=" * 100)
    print(f"  3. MANADSVIS BREAKDOWN -- {best_combo[0]} @ {best_combo[1]} kr")
    print("=" * 100)

    monthly: dict[str, dict] = defaultdict(lambda: {
        "cost": 0.0, "payout": 0.0, "rounds": 0, "hits": 0
    })

    for rd in best_s.round_details:
        month_key = rd["date"][:7]
        monthly[month_key]["cost"] += rd["cost"]
        monthly[month_key]["payout"] += rd["payout"]
        monthly[month_key]["rounds"] += 1
        if rd["hit"]:
            monthly[month_key]["hits"] += 1

    print(f"  {'Manad':<10}  {'Omg':>4}  {'Hits':>5}  {'Kostnad':>10}  {'Utbet':>12}  {'ROI':>8}  {'Netto':>10}")
    print("  " + "-" * 75)

    cumulative_cost = 0.0
    cumulative_payout = 0.0

    for month in sorted(monthly.keys()):
        m = monthly[month]
        roi = ((m["payout"] - m["cost"]) / m["cost"] * 100) if m["cost"] > 0 else 0
        netto = m["payout"] - m["cost"]
        cumulative_cost += m["cost"]
        cumulative_payout += m["payout"]
        print(f"  {month:<10}  {m['rounds']:>4}  {m['hits']:>5}  {m['cost']:>10,.0f}  {m['payout']:>12,.0f}  {roi:>+7.0f}%  {netto:>+10,.0f}")

    cum_roi = ((cumulative_payout - cumulative_cost) / cumulative_cost * 100) if cumulative_cost > 0 else 0
    print("  " + "-" * 75)
    print(f"  {'TOTALT':<10}  {best_s.num_rounds:>4}  {best_s.num_hits:>5}  {cumulative_cost:>10,.0f}  {cumulative_payout:>12,.0f}  {cum_roi:>+7.0f}%  {cumulative_payout - cumulative_cost:>+10,.0f}")
    print()

    # ── 4. Variance analysis ─────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("  4. VARIANSANALYS")
    print("=" * 100)

    for strat in STRATEGY_NAMES:
        print(f"\n  {strat.upper()}:")
        for budget in [200, 1000, 5000]:
            s = results[strat][budget]
            best_pay = f"{s.best_payout:,.0f}" if s.payouts else "0"
            avg_pay = f"{sum(s.payouts)/len(s.payouts):,.0f}" if s.payouts else "0"
            print(f"    Budget {budget:>5} kr: "
                  f"Basta payout={best_pay} kr, "
                  f"Snitt payout={avg_pay} kr, "
                  f"Langsta streak utan hit={s.worst_streak} omgangar, "
                  f"Hits={s.num_hits}")

    print()

    # ── 5. Key insight ───────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("  5. NYCKELINSIKTER")
    print("=" * 100)

    # Find all profitable combos
    profitable = []
    for strat in STRATEGY_NAMES:
        for budget in BUDGETS:
            s = results[strat][budget]
            if s.roi > 0:
                profitable.append((strat, budget, s.roi, s.num_hits))

    if profitable:
        profitable.sort(key=lambda x: -x[2])
        print(f"\n  LONSAMMA KOMBINATIONER ({len(profitable)} st):")
        for strat, budget, roi, hits in profitable[:10]:
            print(f"    {strat:<15} {budget:>5} kr  ROI={roi*100:+.1f}%  Hits={hits}")
    else:
        print("\n  INGA LONSAMMA KOMBINATIONER FUNNA.")
        print("  Alla strategier gar med forlust over hela perioden.")

    # Find break-even strategies
    closest_to_zero = []
    for strat in STRATEGY_NAMES:
        for budget in BUDGETS:
            s = results[strat][budget]
            closest_to_zero.append((strat, budget, abs(s.roi), s.roi))
    closest_to_zero.sort(key=lambda x: x[2])

    print(f"\n  NARMAST BREAK-EVEN:")
    for strat, budget, _, roi in closest_to_zero[:5]:
        print(f"    {strat:<15} {budget:>5} kr  ROI={roi*100:+.1f}%")

    print()

    # ── 6. Selective betting ─────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("  6. SELEKTIV SPEL -- Forbattras ROI genom att bara spela vissa omgangar?")
    print("=" * 100)

    for filter_name, filter_results in selective_results.items():
        print(f"\n  Filter: {filter_name}")

        # Count rounds matching this filter
        first_strat = list(filter_results.values())[0]
        first_budget = list(first_strat.values())[0]
        n_rounds = first_budget.num_rounds

        if n_rounds == 0:
            print(f"    (Inga omgangar matchar filtret)")
            continue

        print(f"    Antal omgangar: {n_rounds}")

        # Show best ROI for this filter
        best_filtered_roi = -999
        best_filtered_combo = ("", 0)
        for strat in STRATEGY_NAMES:
            for budget in BUDGETS:
                s = filter_results[strat][budget]
                if s.roi > best_filtered_roi:
                    best_filtered_roi = s.roi
                    best_filtered_combo = (strat, budget)

        s = filter_results[best_filtered_combo[0]][best_filtered_combo[1]]
        print(f"    Basta: {best_filtered_combo[0]} @ {best_filtered_combo[1]} kr"
              f"  ROI={s.roi*100:+.1f}%, Hits={s.num_hits}/{s.num_rounds}")

        # Compact matrix
        print(f"    {'Strategi':<15}", end="")
        for b in [200, 1000, 5000]:
            print(f"  {b:>6} kr", end="")
        print()

        for strat in STRATEGY_NAMES:
            print(f"    {strat:<15}", end="")
            for budget in [200, 1000, 5000]:
                s = filter_results[strat][budget]
                print(f"  {s.roi*100:>+6.0f}%", end="")
            print()

    print()

    # ── 7. Recommendation ────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("  7. REKOMMENDATION")
    print("=" * 100)

    # Find best overall (selective included)
    all_combos = []
    for strat in STRATEGY_NAMES:
        for budget in BUDGETS:
            s = results[strat][budget]
            all_combos.append({
                "filter": "alla",
                "strategy": strat,
                "budget": budget,
                "roi": s.roi,
                "hits": s.num_hits,
                "rounds": s.num_rounds,
                "hit_rate": s.hit_rate,
                "netto": s.total_payout - s.total_cost,
            })

    for filter_name, fr in selective_results.items():
        for strat in STRATEGY_NAMES:
            for budget in BUDGETS:
                s = fr[strat][budget]
                all_combos.append({
                    "filter": filter_name,
                    "strategy": strat,
                    "budget": budget,
                    "roi": s.roi,
                    "hits": s.num_hits,
                    "rounds": s.num_rounds,
                    "hit_rate": s.hit_rate,
                    "netto": s.total_payout - s.total_cost,
                })

    all_combos.sort(key=lambda x: -x["roi"])
    top5 = all_combos[:5]

    print("\n  TOPP 5 KOMBINATIONER (ROI):")
    for i, c in enumerate(top5, 1):
        print(f"    {i}. [{c['filter']}] {c['strategy']} @ {c['budget']} kr"
              f"  ROI={c['roi']*100:+.1f}%"
              f"  Hits={c['hits']}/{c['rounds']}"
              f"  Netto={c['netto']:+,.0f} kr")

    # Recommendation text
    if top5 and top5[0]["roi"] > 0:
        best = top5[0]
        print(f"\n  --> FOKUSERA PA: {best['strategy']} strategi, {best['budget']} kr budget")
        if best["filter"] != "alla":
            print(f"      med selektiv spel: {best['filter']}")
        print(f"      Forvantad ROI: {best['roi']*100:+.1f}% ({best['hits']} hits pa {best['rounds']} omgangar)")
    else:
        print("\n  --> INGEN STRATEGI AR KONSEKVENT LONSAM.")
        print("      Modellen behover forbattras (hogre top-1 accuracy) innan V75/V85-spel blir lonsamt.")
        print("      Overvaag att:")
        print("        - Forbattra modellens top-1 fran ~24% till >30%")
        print("        - Anvanda selektiv spel (bara spela 'sakra' omgangar)")
        print("        - Fokusera pa skrall-dagar med hog forvantad utdelning")

    print("\n" + "=" * 100)


def build_json_output(
    results: dict[str, dict[int, BacktestSummary]],
    round_analyses: list[RoundAnalysis],
    selective_results: dict[str, dict[str, dict[int, BacktestSummary]]],
) -> dict:
    """Build JSON output for roi_results.json."""

    output = {
        "metadata": {
            "num_rounds": len(round_analyses),
            "v75_rounds": sum(1 for ra in round_analyses if ra.game_type == "V75"),
            "v85_rounds": sum(1 for ra in round_analyses if ra.game_type == "V85"),
            "period_start": min(ra.round_date.isoformat() for ra in round_analyses) if round_analyses else "",
            "period_end": max(ra.round_date.isoformat() for ra in round_analyses) if round_analyses else "",
            "budgets": BUDGETS,
            "strategies": STRATEGY_NAMES,
        },
        "results": {},
        "selective_results": {},
        "best_combos": [],
    }

    # Main results
    for strat in STRATEGY_NAMES:
        output["results"][strat] = {}
        for budget in BUDGETS:
            s = results[strat][budget]
            output["results"][strat][str(budget)] = {
                "roi": round(s.roi, 4),
                "hit_rate": round(s.hit_rate, 4),
                "num_hits": s.num_hits,
                "num_rounds": s.num_rounds,
                "total_cost": round(s.total_cost, 2),
                "total_payout": round(s.total_payout, 2),
                "netto": round(s.total_payout - s.total_cost, 2),
                "best_payout": round(s.best_payout, 2),
                "worst_streak": s.worst_streak,
                "hit_dates": s.hit_rounds,
            }

    # Selective results
    for filter_name, fr in selective_results.items():
        output["selective_results"][filter_name] = {}
        for strat in STRATEGY_NAMES:
            output["selective_results"][filter_name][strat] = {}
            for budget in BUDGETS:
                s = fr[strat][budget]
                output["selective_results"][filter_name][strat][str(budget)] = {
                    "roi": round(s.roi, 4),
                    "hit_rate": round(s.hit_rate, 4),
                    "num_hits": s.num_hits,
                    "num_rounds": s.num_rounds,
                    "total_cost": round(s.total_cost, 2),
                    "total_payout": round(s.total_payout, 2),
                    "netto": round(s.total_payout - s.total_cost, 2),
                }

    # Best combos
    all_combos = []
    for strat in STRATEGY_NAMES:
        for budget in BUDGETS:
            s = results[strat][budget]
            all_combos.append({
                "filter": "alla_omgangar",
                "strategy": strat,
                "budget": budget,
                "roi": round(s.roi, 4),
                "hits": s.num_hits,
                "rounds": s.num_rounds,
                "netto": round(s.total_payout - s.total_cost, 2),
            })
    for filter_name, fr in selective_results.items():
        for strat in STRATEGY_NAMES:
            for budget in BUDGETS:
                s = fr[strat][budget]
                all_combos.append({
                    "filter": filter_name,
                    "strategy": strat,
                    "budget": budget,
                    "roi": round(s.roi, 4),
                    "hits": s.num_hits,
                    "rounds": s.num_rounds,
                    "netto": round(s.total_payout - s.total_cost, 2),
                })

    all_combos.sort(key=lambda x: -x["roi"])
    output["best_combos"] = all_combos[:20]

    return output


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    start_time = time.time()

    # 1. Load data
    logger.info("Loading historical rounds...")
    all_rounds = await load_rounds()
    logger.info(f"Loaded {len(all_rounds)} rounds in {time.time()-start_time:.0f}s")

    # 2. Analyze each round
    logger.info("Analyzing rounds with CompositeAnalyzer...")
    round_analyses: list[RoundAnalysis] = []
    skipped = 0

    for i, gr in enumerate(all_rounds):
        ra = analyze_round(gr)
        if ra:
            round_analyses.append(ra)
        else:
            skipped += 1
        if (i + 1) % 10 == 0:
            logger.info(f"  Analyzed {i+1}/{len(all_rounds)} rounds...")

    logger.info(f"Analyzed {len(round_analyses)} rounds ({skipped} skipped)")

    if not round_analyses:
        logger.error("No valid rounds to analyze!")
        return

    # 3. Run main backtest
    logger.info("Running backtest (all strategies x all budgets)...")
    results = run_backtest(round_analyses)

    # 4. Run selective backtests
    logger.info("Running selective backtests...")
    selective_results: dict[str, dict[str, dict[int, BacktestSummary]]] = {}

    for filter_name, filter_fn in SELECTIVE_FILTERS.items():
        if filter_name == "alla_omgangar":
            continue  # Already have this
        filtered = [ra for ra in round_analyses if filter_fn(ra)]
        logger.info(f"  {filter_name}: {len(filtered)}/{len(round_analyses)} rounds")
        if filtered:
            selective_results[filter_name] = run_backtest(filtered)
        else:
            # Empty results
            empty = {}
            for strat in STRATEGY_NAMES:
                empty[strat] = {}
                for budget in BUDGETS:
                    empty[strat][budget] = BacktestSummary(
                        strategy=strat, budget=budget
                    )
            selective_results[filter_name] = empty

    # 5. Print report
    print_report(results, round_analyses, selective_results)

    # 6. Save JSON results
    output_path = Path(__file__).parent / "roi_results.json"
    json_output = build_json_output(results, round_analyses, selective_results)
    output_path.write_text(json.dumps(json_output, indent=2, ensure_ascii=False))
    logger.info(f"Results saved to {output_path}")

    elapsed = time.time() - start_time
    logger.info(f"Total time: {elapsed:.0f}s ({elapsed/60:.1f} min)")


if __name__ == "__main__":
    asyncio.run(main())
