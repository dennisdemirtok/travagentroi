#!/usr/bin/env python3
"""Empirical System Optimizer -- tests many system building strategies
on historical V75/V85 data to find the optimal approach.

For each historical round:
  1. Load from cache (ATGClient)
  2. Temporal filtering (remove future starts, neutralize odds)
  3. Run CompositeAnalyzer to score all horses
  4. Try MANY different system configurations
  5. Check if the system "hit" (all winners covered)
  6. If hit: profit = dividend - cost
  7. Track cumulative results per strategy

Output: ranking table of all strategies by ROI and hit rate.
"""

import asyncio
import json
import glob
import re
import sys
import random
import time
import math
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.data.models import GameRound, Race, StartMethod
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.config import DEFAULT_CONFIG

random.seed(42)

CACHE_DIR = Path(__file__).parent / "cache"
MAX_ROUNDS = 150
ROW_PRICE = 0.50
START_YEAR = 2022  # go back further for larger sample


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def find_round_ids(start_year: int = START_YEAR) -> list[dict]:
    """Scan calendar cache files to find finished V75/V85 rounds."""
    cal_re = re.compile(r"^(\d{4})-\d{2}-\d{2}_[a-f0-9]+\.json$")
    rounds: list[dict] = []

    for fp in sorted(glob.glob(str(CACHE_DIR / "*.json"))):
        fname = Path(fp).name
        m = cal_re.match(fname)
        if not m:
            continue
        year = int(m.group(1))
        if year < start_year:
            continue

        try:
            with open(fp) as f:
                data = json.load(f)
        except Exception:
            continue

        games = data.get("games", {})
        if not isinstance(games, dict):
            continue

        for game_type in ("V75", "V85"):
            for g in games.get(game_type, []):
                if g.get("status") == "results" and g.get("races"):
                    rounds.append({
                        "game_type": game_type,
                        "game_id": g["id"],
                    })

    return rounds


async def load_round(client: ATGClient, info: dict) -> GameRound | None:
    """Load a single round from cache via ATGClient."""
    try:
        parts = info["game_id"].split("_")
        gt = parts[0]
        day = date.fromisoformat(parts[1])
        gr = await client.fetch_full_round(gt, day)
        if gr and gr.is_finished and gr.races:
            return gr
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Temporal filtering (anti-leakage)
# ---------------------------------------------------------------------------

def apply_temporal_filtering(gr: GameRound) -> None:
    """Remove future starts and neutralize closing odds."""
    for race in gr.races:
        race_date = race.race_date
        for entry in race.entries:
            original = entry.horse.past_starts
            filtered = [s for s in original if s.start_date < race_date]
            if len(filtered) < len(original):
                entry.horse.past_starts = filtered
            entry.horse.recompute_career_from_starts()
            # Neutralize closing odds -- model must stand on its own
            entry.bet_percentage = None
            entry.odds = None


# ---------------------------------------------------------------------------
# Per-leg analysis data (computed once per round)
# ---------------------------------------------------------------------------

@dataclass
class LegInfo:
    """Pre-computed data for one leg in a round."""
    leg_index: int           # 0-based
    race_number: int
    num_starters: int
    winner_pp: int           # post_position of actual winner
    model_ranking: list[int] # post_positions in model score order
    scores: list[float]      # super_scores in descending order
    gap_1_2: float           # score gap rank1 vs rank2
    gap_1_3: float           # score gap rank1 vs rank3
    upset_risk: float        # race.upset_risk (0-100)
    confidence: float        # how confident (gap-based)
    is_volt: bool
    is_stolopp: bool
    top_driver_win_pct: float  # win pct of the driver of model #1


def compute_leg_infos(gr: GameRound) -> list[LegInfo]:
    """Build LegInfo for each race in the round."""
    legs = []
    for idx, race in enumerate(gr.races):
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score,
            reverse=True,
        )
        if not sorted_entries:
            continue

        winner_pp = race.result_order[0] if race.result_order else -1
        model_ranking = [e.post_position for e in sorted_entries]
        scores = [e.super_score for e in sorted_entries]

        gap_1_2 = scores[0] - scores[1] if len(scores) >= 2 else 0.0
        gap_1_3 = scores[0] - scores[2] if len(scores) >= 3 else 0.0

        # Upset risk from analyzer
        upset = race.upset_risk

        # Confidence: combination of gap and upset
        conf = max(0, min(100, 50 + gap_1_2 * 2 - upset * 0.3))

        # Start method
        is_volt = race.start_method == StartMethod.VOLT

        # Stolopp detection
        rn_lower = (race.race_name or "").lower()
        rt_lower = (race.race_type or "").lower()
        is_stolopp = any(
            w in rn_lower or w in rt_lower
            for w in ("stolopp", "storlopp", "stl", "final", "milen",
                       "derby", "grand", "elitlopp", "kriterium", "oaks")
        )

        # Driver win pct for model #1
        top_driver_pct = sorted_entries[0].driver_win_pct if sorted_entries else 0.0

        legs.append(LegInfo(
            leg_index=idx,
            race_number=race.race_number,
            num_starters=len(sorted_entries),
            winner_pp=winner_pp,
            model_ranking=model_ranking,
            scores=scores,
            gap_1_2=gap_1_2,
            gap_1_3=gap_1_3,
            upset_risk=upset,
            confidence=conf,
            is_volt=is_volt,
            is_stolopp=is_stolopp,
            top_driver_win_pct=top_driver_pct,
        ))
    return legs


# ---------------------------------------------------------------------------
# System check: did a system configuration hit?
# ---------------------------------------------------------------------------

def count_correct_legs(legs: list[LegInfo], picks_per_leg: list[int]) -> int:
    """Count how many legs have the winner covered.

    picks_per_leg[i] = how many top-ranked horses to include for leg i.
    """
    if len(picks_per_leg) != len(legs):
        return 0
    correct = 0
    for leg, n_picks in zip(legs, picks_per_leg):
        if leg.winner_pp < 0:
            continue
        covered = leg.model_ranking[:n_picks]
        if leg.winner_pp in covered:
            correct += 1
    return correct


def system_hit(legs: list[LegInfo], picks_per_leg: list[int]) -> bool:
    """Check if all winners are covered by the system."""
    return count_correct_legs(legs, picks_per_leg) == len(legs)


def system_cost(picks_per_leg: list[int]) -> float:
    """Total rows * row price."""
    rows = 1
    for n in picks_per_leg:
        rows *= n
    return rows * ROW_PRICE


# ---------------------------------------------------------------------------
# Strategy definitions
# ---------------------------------------------------------------------------

def assign_picks_fixed_spikes(
    legs: list[LegInfo],
    n_spikes: int,
    rest_width: int,
    sort_key: str = "gap",
    budget: float | None = None,
) -> list[int]:
    """Assign 1 pick to best N legs (spikes) and rest_width to others.

    sort_key determines how to choose spike legs:
      'gap': highest gap_1_2
      'confidence': highest confidence
      'upset_low': lowest upset_risk
    """
    n = len(legs)
    if n_spikes > n:
        n_spikes = n

    # Sort legs to find spike candidates
    if sort_key == "gap":
        order = sorted(range(n), key=lambda i: legs[i].gap_1_2, reverse=True)
    elif sort_key == "confidence":
        order = sorted(range(n), key=lambda i: legs[i].confidence, reverse=True)
    elif sort_key == "upset_low":
        order = sorted(range(n), key=lambda i: legs[i].upset_risk)
    else:
        order = list(range(n))

    spike_indices = set(order[:n_spikes])
    picks = [1 if i in spike_indices else rest_width for i in range(n)]

    # Budget constraint: reduce widest legs if over budget
    if budget is not None:
        cost = system_cost(picks)
        while cost > budget:
            # Find widest non-spike leg
            widest_idx = -1
            widest_val = 0
            for i in range(n):
                if i not in spike_indices and picks[i] > widest_val:
                    widest_val = picks[i]
                    widest_idx = i
            if widest_idx < 0 or picks[widest_idx] <= 2:
                break
            picks[widest_idx] -= 1
            cost = system_cost(picks)

    return picks


def assign_picks_mixed(
    legs: list[LegInfo],
    n_spikes: int,
    n_short: int,
    short_width: int,
    rest_width: int,
    sort_key: str = "gap",
    budget: float | None = None,
) -> list[int]:
    """Spike N legs, short picks for M legs, rest_width for remainder."""
    n = len(legs)

    if sort_key == "gap":
        order = sorted(range(n), key=lambda i: legs[i].gap_1_2, reverse=True)
    elif sort_key == "confidence":
        order = sorted(range(n), key=lambda i: legs[i].confidence, reverse=True)
    elif sort_key == "upset_low":
        order = sorted(range(n), key=lambda i: legs[i].upset_risk)
    else:
        order = list(range(n))

    spike_set = set(order[:n_spikes])
    short_set = set(order[n_spikes:n_spikes + n_short])

    picks = []
    for i in range(n):
        if i in spike_set:
            picks.append(1)
        elif i in short_set:
            picks.append(short_width)
        else:
            picks.append(rest_width)

    if budget is not None:
        cost = system_cost(picks)
        while cost > budget:
            widest_idx = -1
            widest_val = 0
            for i in range(n):
                if i not in spike_set and picks[i] > widest_val:
                    widest_val = picks[i]
                    widest_idx = i
            if widest_idx < 0 or picks[widest_idx] <= 2:
                break
            picks[widest_idx] -= 1
            cost = system_cost(picks)

    return picks


def assign_picks_variable(legs: list[LegInfo], budget: float | None = None) -> list[int]:
    """Variable picks based on gap thresholds.

    Gap > 15 => spike (1)
    Gap > 10 => 2 picks
    Gap > 6  => 3 picks
    Else     => 4 picks (capped at 5 for very open races)
    """
    picks = []
    for leg in legs:
        if leg.gap_1_2 > 15:
            picks.append(1)
        elif leg.gap_1_2 > 10:
            picks.append(2)
        elif leg.gap_1_2 > 6:
            picks.append(3)
        elif leg.upset_risk > 60:
            picks.append(5)
        else:
            picks.append(4)

    if budget is not None:
        cost = system_cost(picks)
        while cost > budget:
            # Reduce widest non-spike leg
            widest_idx = max(range(len(picks)), key=lambda i: picks[i])
            if picks[widest_idx] <= 2:
                break
            picks[widest_idx] -= 1
            cost = system_cost(picks)

    return picks


def assign_picks_upset_variable(legs: list[LegInfo], budget: float | None = None) -> list[int]:
    """Spike low-upset races, go wide in high-upset ones.

    Upset < 20 => spike (1)
    Upset < 35 => 2 picks
    Upset < 50 => 3 picks
    Upset < 65 => 4 picks
    Else       => 5 picks
    """
    picks = []
    for leg in legs:
        if leg.upset_risk < 20:
            picks.append(1)
        elif leg.upset_risk < 35:
            picks.append(2)
        elif leg.upset_risk < 50:
            picks.append(3)
        elif leg.upset_risk < 65:
            picks.append(4)
        else:
            picks.append(5)

    if budget is not None:
        cost = system_cost(picks)
        while cost > budget:
            widest_idx = max(range(len(picks)), key=lambda i: picks[i])
            if picks[widest_idx] <= 2:
                break
            picks[widest_idx] -= 1
            cost = system_cost(picks)

    return picks


def assign_picks_driver_spike(legs: list[LegInfo], budget: float | None = None) -> list[int]:
    """Spike when top driver has high win%, avoid spiking volt/stolopp.

    Spike if: driver_win_pct > 15% AND NOT volt AND NOT stolopp AND gap > 5
    Otherwise: 3 picks, widened to 4-5 for high-upset legs.
    """
    picks = []
    for leg in legs:
        can_spike = (
            leg.top_driver_win_pct > 0.15
            and not leg.is_volt
            and not leg.is_stolopp
            and leg.gap_1_2 > 5
        )
        if can_spike:
            picks.append(1)
        elif leg.upset_risk > 55:
            picks.append(4)
        else:
            picks.append(3)

    if budget is not None:
        cost = system_cost(picks)
        while cost > budget:
            widest_idx = max(range(len(picks)), key=lambda i: picks[i])
            if picks[widest_idx] <= 2:
                break
            picks[widest_idx] -= 1
            cost = system_cost(picks)

    return picks


# ---------------------------------------------------------------------------
# Build all strategy configs to test
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    name: str
    budget: float
    assign_fn: str   # function name or key
    params: dict = field(default_factory=dict)


def build_strategies() -> list[StrategyConfig]:
    """Generate all strategy configurations to test."""
    strategies = []
    budgets = [100, 200, 500, 1000]

    for budget in budgets:
        # --- Fixed spike strategies ---
        for n_spikes in range(0, 5):
            for rest_width in range(2, 7):
                for sort_key in ("gap", "confidence", "upset_low"):
                    name = f"{n_spikes}S+rest{rest_width}|{sort_key}|{budget}kr"
                    strategies.append(StrategyConfig(
                        name=name,
                        budget=budget,
                        assign_fn="fixed_spikes",
                        params={
                            "n_spikes": n_spikes,
                            "rest_width": rest_width,
                            "sort_key": sort_key,
                        },
                    ))

        # --- Mixed strategies ---
        # 1S + 2x2 + rest3
        for sort_key in ("gap", "confidence", "upset_low"):
            strategies.append(StrategyConfig(
                name=f"1S+2x2+rest3|{sort_key}|{budget}kr",
                budget=budget,
                assign_fn="mixed",
                params={
                    "n_spikes": 1, "n_short": 2,
                    "short_width": 2, "rest_width": 3,
                    "sort_key": sort_key,
                },
            ))
            # 2S + 2x2 + rest3
            strategies.append(StrategyConfig(
                name=f"2S+2x2+rest3|{sort_key}|{budget}kr",
                budget=budget,
                assign_fn="mixed",
                params={
                    "n_spikes": 2, "n_short": 2,
                    "short_width": 2, "rest_width": 3,
                    "sort_key": sort_key,
                },
            ))
            # 1S + 3x2 + rest4
            strategies.append(StrategyConfig(
                name=f"1S+3x2+rest4|{sort_key}|{budget}kr",
                budget=budget,
                assign_fn="mixed",
                params={
                    "n_spikes": 1, "n_short": 3,
                    "short_width": 2, "rest_width": 4,
                    "sort_key": sort_key,
                },
            ))
            # 2S + 1x2 + rest4
            strategies.append(StrategyConfig(
                name=f"2S+1x2+rest4|{sort_key}|{budget}kr",
                budget=budget,
                assign_fn="mixed",
                params={
                    "n_spikes": 2, "n_short": 1,
                    "short_width": 2, "rest_width": 4,
                    "sort_key": sort_key,
                },
            ))

        # --- Variable strategies ---
        strategies.append(StrategyConfig(
            name=f"variable_gap|{budget}kr",
            budget=budget,
            assign_fn="variable",
            params={},
        ))
        strategies.append(StrategyConfig(
            name=f"upset_variable|{budget}kr",
            budget=budget,
            assign_fn="upset_variable",
            params={},
        ))
        strategies.append(StrategyConfig(
            name=f"driver_spike|{budget}kr",
            budget=budget,
            assign_fn="driver_spike",
            params={},
        ))

    return strategies


def apply_strategy(strat: StrategyConfig, legs: list[LegInfo]) -> list[int]:
    """Apply a strategy config to a set of legs, returning picks per leg."""
    budget = strat.budget
    fn = strat.assign_fn
    p = strat.params

    if fn == "fixed_spikes":
        return assign_picks_fixed_spikes(
            legs, p["n_spikes"], p["rest_width"], p["sort_key"], budget,
        )
    elif fn == "mixed":
        return assign_picks_mixed(
            legs, p["n_spikes"], p["n_short"],
            p["short_width"], p["rest_width"],
            p["sort_key"], budget,
        )
    elif fn == "variable":
        return assign_picks_variable(legs, budget)
    elif fn == "upset_variable":
        return assign_picks_upset_variable(legs, budget)
    elif fn == "driver_spike":
        return assign_picks_driver_spike(legs, budget)
    else:
        raise ValueError(f"Unknown strategy function: {fn}")


# ---------------------------------------------------------------------------
# Results tracking
# ---------------------------------------------------------------------------

@dataclass
class StrategyResult:
    name: str
    rounds_tested: int = 0
    hits: int = 0           # all legs correct
    partial_n1: int = 0     # n-1 legs correct
    partial_n2: int = 0     # n-2 legs correct
    total_cost: float = 0.0
    total_payout: float = 0.0
    total_partial_payout: float = 0.0  # payouts from n-1 tier
    costs: list[float] = field(default_factory=list)
    payouts: list[float] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return self.hits / self.rounds_tested if self.rounds_tested else 0.0

    @property
    def partial_n1_rate(self) -> float:
        return self.partial_n1 / self.rounds_tested if self.rounds_tested else 0.0

    @property
    def roi(self) -> float:
        if self.total_cost <= 0:
            return 0.0
        return (self.total_payout - self.total_cost) / self.total_cost

    @property
    def roi_with_partials(self) -> float:
        """ROI including n-1 partial payouts."""
        if self.total_cost <= 0:
            return 0.0
        return (self.total_payout + self.total_partial_payout - self.total_cost) / self.total_cost

    @property
    def avg_cost_per_round(self) -> float:
        return self.total_cost / self.rounds_tested if self.rounds_tested else 0.0

    @property
    def avg_profit_per_round(self) -> float:
        return (self.total_payout - self.total_cost) / self.rounds_tested if self.rounds_tested else 0.0

    @property
    def avg_payout_when_hit(self) -> float:
        return self.total_payout / self.hits if self.hits else 0.0


# ---------------------------------------------------------------------------
# Main optimizer
# ---------------------------------------------------------------------------

async def main():
    t0 = time.time()
    client = ATGClient()
    analyzer = CompositeAnalyzer(DEFAULT_CONFIG)

    # ---- Step 1: Find rounds ----
    print("=" * 80)
    print("  EMPIRICAL SYSTEM OPTIMIZER")
    print("  Testing system configurations on historical V75/V85 data")
    print("=" * 80)
    print()

    round_infos = find_round_ids(start_year=START_YEAR)
    random.shuffle(round_infos)
    round_infos = round_infos[:MAX_ROUNDS]
    print(f"Found {len(round_infos)} round candidates (max {MAX_ROUNDS})")

    # ---- Step 2: Load and prepare rounds ----
    print("\nLoading rounds from cache...")
    rounds: list[GameRound] = []
    legs_per_round: list[list[LegInfo]] = []
    dividends_per_round: list[float] = []      # top tier (all correct)
    dividends_n1_per_round: list[float] = []   # n-1 tier
    num_legs_per_round: list[int] = []
    game_types: list[str] = []

    loaded = 0
    skipped_no_div = 0
    skipped_load = 0

    for i, info in enumerate(round_infos):
        gr = await load_round(client, info)
        if not gr:
            skipped_load += 1
            continue

        # Check for result data and dividends
        has_results = all(r.result_order for r in gr.races)
        if not has_results:
            skipped_load += 1
            continue

        # Get top-tier dividend
        num_legs = len(gr.races)
        top_div = gr.dividends.get(num_legs, 0.0)
        if top_div <= 0:
            skipped_no_div += 1
            continue

        # Apply temporal filtering
        apply_temporal_filtering(gr)

        # Run analysis
        analyzer.analyze_round(gr)

        # Compute leg infos
        leg_infos = compute_leg_infos(gr)
        if len(leg_infos) != num_legs:
            skipped_load += 1
            continue

        # n-1 dividend (e.g. 6 correct for V75, 7 correct for V85)
        n1_div = gr.dividends.get(num_legs - 1, 0.0)

        rounds.append(gr)
        legs_per_round.append(leg_infos)
        dividends_per_round.append(top_div)
        dividends_n1_per_round.append(n1_div)
        num_legs_per_round.append(num_legs)
        game_types.append(gr.game_type)
        loaded += 1

        if loaded % 10 == 0:
            elapsed = time.time() - t0
            print(f"  Loaded {loaded} rounds ({elapsed:.0f}s elapsed)...")

    print(f"\nLoaded {loaded} rounds successfully")
    print(f"  Skipped: {skipped_load} load failures, {skipped_no_div} missing dividends")

    v75_count = sum(1 for g in game_types if g == "V75")
    v85_count = sum(1 for g in game_types if g == "V85")
    print(f"  V75: {v75_count}, V85: {v85_count}")
    if dividends_per_round:
        avg_div = sum(dividends_per_round) / len(dividends_per_round)
        med_div = sorted(dividends_per_round)[len(dividends_per_round) // 2]
        print(f"  Avg dividend: {avg_div:,.0f} kr, Median: {med_div:,.0f} kr")
    print()

    if loaded < 5:
        print("Too few rounds loaded, aborting.")
        return

    # ---- Step 3: Build strategies ----
    all_strategies = build_strategies()
    print(f"Testing {len(all_strategies)} strategy configurations...")

    # ---- Step 4: Run all strategies on all rounds ----
    results: dict[str, StrategyResult] = {}
    tested = 0

    for strat in all_strategies:
        sr = StrategyResult(name=strat.name)

        for r_idx in range(loaded):
            legs = legs_per_round[r_idx]
            dividend = dividends_per_round[r_idx]
            n1_div = dividends_n1_per_round[r_idx]
            n_legs = num_legs_per_round[r_idx]

            picks = apply_strategy(strat, legs)
            cost = system_cost(picks)

            # Skip if cost exceeds budget significantly (allow small overshoot)
            if cost > strat.budget * 1.1:
                continue

            sr.rounds_tested += 1
            sr.total_cost += cost
            sr.costs.append(cost)

            correct = count_correct_legs(legs, picks)
            if correct == n_legs:
                sr.hits += 1
                sr.total_payout += dividend
                sr.payouts.append(dividend)
            elif correct == n_legs - 1:
                sr.partial_n1 += 1
                sr.total_partial_payout += n1_div
                sr.payouts.append(0.0)
            elif correct == n_legs - 2:
                sr.partial_n2 += 1
                sr.payouts.append(0.0)
            else:
                sr.payouts.append(0.0)

        results[strat.name] = sr
        tested += 1

    elapsed = time.time() - t0
    print(f"Tested {tested} strategies in {elapsed:.0f}s\n")

    # ---- Step 5: Print results ----
    print_results(results, loaded, dividends_per_round)

    # ---- Step 6: Print top strategies per budget ----
    print_budget_analysis(results)

    # ---- Step 7: Print strategy type comparison ----
    print_strategy_type_comparison(results, loaded)

    # ---- Step 8: Print spike count analysis ----
    print_spike_analysis(results)

    # ---- Step 9: Print breakeven analysis ----
    print_breakeven_analysis(results, loaded, dividends_per_round)

    # ---- Step 10: Key findings summary ----
    print_key_findings(results, loaded)

    elapsed = time.time() - t0
    print(f"\nTotal runtime: {elapsed:.0f}s")


# ---------------------------------------------------------------------------
# Output tables
# ---------------------------------------------------------------------------

def print_results(
    results: dict[str, StrategyResult],
    total_rounds: int,
    dividends: list[float],
):
    """Print the main results table ranked by ROI (incl. partials)."""
    print("=" * 140)
    print("  TOP 50 STRATEGIES BY ROI (including n-1 partial wins)")
    print("=" * 140)

    # Filter to strategies with at least some rounds and some hits or partials
    valid = [r for r in results.values()
             if r.rounds_tested >= total_rounds * 0.5 and (r.hits > 0 or r.partial_n1 > 0)]
    if not valid:
        valid = [r for r in results.values() if r.hits > 0]
    if not valid:
        print("  No strategies hit any rounds!")
        valid = sorted(results.values(), key=lambda r: r.rounds_tested, reverse=True)[:20]

    # Sort by ROI including partials
    valid.sort(key=lambda r: r.roi_with_partials, reverse=True)

    header = (
        f"  {'#':>3} {'Strategy':<40} {'Rnds':>5} {'Hits':>4} {'N-1':>4} {'N-2':>4} "
        f"{'HitRate':>7} {'N-1Rate':>7} {'AvgCost':>8} {'TotPay':>12} "
        f"{'ROI':>7} {'ROI+N1':>7} {'AvgProfit':>10}"
    )
    print(header)
    print("  " + "-" * 138)

    for i, sr in enumerate(valid[:50], 1):
        print(
            f"  {i:>3} {sr.name:<40} {sr.rounds_tested:>5} {sr.hits:>4} "
            f"{sr.partial_n1:>4} {sr.partial_n2:>4} "
            f"{sr.hit_rate:>6.1%} {sr.partial_n1_rate:>6.1%} "
            f"{sr.avg_cost_per_round:>7.0f}kr "
            f"{sr.total_payout:>11,.0f}kr "
            f"{sr.roi:>+6.0%} {sr.roi_with_partials:>+6.0%} "
            f"{sr.avg_profit_per_round:>9.0f}kr"
        )


def print_budget_analysis(results: dict[str, StrategyResult]):
    """Show best strategy per budget level."""
    print("\n\n" + "=" * 100)
    print("  BEST STRATEGY PER BUDGET")
    print("=" * 100)

    for budget in [100, 200, 500, 1000]:
        budget_strats = [
            r for r in results.values()
            if f"|{budget}kr" in r.name and r.rounds_tested > 0
        ]
        if not budget_strats:
            continue

        # Best by ROI (only among those with hits or partials)
        with_hits = [r for r in budget_strats if r.hits > 0 or r.partial_n1 > 0]
        if with_hits:
            by_roi = sorted(with_hits, key=lambda r: r.roi_with_partials, reverse=True)
            by_hitrate = sorted(with_hits, key=lambda r: r.hit_rate, reverse=True)

            print(f"\n  --- Budget: {budget} kr ---")
            b = by_roi[0]
            print(f"  Best ROI:      {b.name:<40} ROI={b.roi_with_partials:+.0%}  "
                  f"hits={b.hits}+{b.partial_n1}n1/{b.rounds_tested} "
                  f"({b.hit_rate:.1%})  "
                  f"avg_profit={b.avg_profit_per_round:.0f}kr")
            b = by_hitrate[0]
            print(f"  Best Hit Rate: {b.name:<40} ROI={b.roi_with_partials:+.0%}  "
                  f"hits={b.hits}+{b.partial_n1}n1/{b.rounds_tested} "
                  f"({b.hit_rate:.1%})  "
                  f"avg_profit={b.avg_profit_per_round:.0f}kr")

            # Show top 5 by ROI
            print(f"\n  Top 5 by ROI (incl. n-1) for {budget}kr:")
            print(f"    {'Strategy':<40} {'Hits':>4} {'N-1':>4} {'HitRate':>7} {'ROI+N1':>7} {'AvgProfit':>10}")
            for r in by_roi[:5]:
                print(f"    {r.name:<40} {r.hits:>4} {r.partial_n1:>4} "
                      f"{r.hit_rate:>6.1%} {r.roi_with_partials:>+6.0%} {r.avg_profit_per_round:>9.0f}kr")
        else:
            print(f"\n  --- Budget: {budget} kr --- No hits!")


def print_strategy_type_comparison(results: dict[str, StrategyResult], total_rounds: int):
    """Compare strategy families (fixed spike, mixed, variable, etc.)."""
    print("\n\n" + "=" * 100)
    print("  STRATEGY TYPE COMPARISON (aggregated across budgets)")
    print("=" * 100)

    families = {
        "0 spikes (pure gardering)": [],
        "1 spike + rest uniform": [],
        "2 spikes + rest uniform": [],
        "3 spikes + rest uniform": [],
        "4 spikes + rest uniform": [],
        "Mixed (spikes + short + wide)": [],
        "Variable (gap-based)": [],
        "Upset-variable": [],
        "Driver spike": [],
    }

    for r in results.values():
        if r.rounds_tested < total_rounds * 0.3:
            continue
        name = r.name
        if name.startswith("0S+rest"):
            families["0 spikes (pure gardering)"].append(r)
        elif name.startswith("1S+rest"):
            families["1 spike + rest uniform"].append(r)
        elif name.startswith("2S+rest"):
            families["2 spikes + rest uniform"].append(r)
        elif name.startswith("3S+rest"):
            families["3 spikes + rest uniform"].append(r)
        elif name.startswith("4S+rest"):
            families["4 spikes + rest uniform"].append(r)
        elif "x2+rest" in name or "mixed" in name.lower():
            families["Mixed (spikes + short + wide)"].append(r)
        elif name.startswith("variable"):
            families["Variable (gap-based)"].append(r)
        elif name.startswith("upset"):
            families["Upset-variable"].append(r)
        elif name.startswith("driver"):
            families["Driver spike"].append(r)

    print(f"\n  {'Family':<35} {'Configs':>7} {'AvgHitRate':>10} {'AvgROI':>8} {'BestROI':>8} {'MaxHitRate':>10}")
    print("  " + "-" * 80)

    for fam_name, strats in families.items():
        if not strats:
            continue
        with_hits = [s for s in strats if s.hits > 0]
        avg_hr = sum(s.hit_rate for s in strats) / len(strats) if strats else 0
        avg_roi = sum(s.roi for s in with_hits) / len(with_hits) if with_hits else -1
        best_roi = max((s.roi for s in with_hits), default=-1)
        max_hr = max(s.hit_rate for s in strats) if strats else 0

        print(
            f"  {fam_name:<35} {len(strats):>7} {avg_hr:>9.1%} "
            f"{avg_roi:>+7.0%} {best_roi:>+7.0%} {max_hr:>9.1%}"
        )


def print_spike_analysis(results: dict[str, StrategyResult]):
    """Analyze which spike count works best across all configs."""
    print("\n\n" + "=" * 100)
    print("  SPIKE COUNT ANALYSIS")
    print("  Which number of spikes works best overall?")
    print("=" * 100)

    # Group fixed-spike strategies by spike count
    spike_groups: dict[int, list[StrategyResult]] = defaultdict(list)

    for r in results.values():
        if r.rounds_tested == 0:
            continue
        m = re.match(r"^(\d)S\+rest", r.name)
        if m:
            n_spikes = int(m.group(1))
            spike_groups[n_spikes].append(r)

    print(f"\n  {'Spikes':>6} {'Configs':>8} {'WithHits':>8} {'AvgHitRate':>10} {'AvgROI':>8} {'BestROI':>8} {'BestStrategy':<45}")
    print("  " + "-" * 100)

    for n_spikes in sorted(spike_groups.keys()):
        strats = spike_groups[n_spikes]
        with_hits = [s for s in strats if s.hits > 0]
        avg_hr = sum(s.hit_rate for s in strats) / len(strats)
        avg_roi = sum(s.roi for s in with_hits) / len(with_hits) if with_hits else -1
        best = max(with_hits, key=lambda s: s.roi) if with_hits else None
        best_roi = best.roi if best else -1
        best_name = best.name if best else "-"

        print(
            f"  {n_spikes:>6} {len(strats):>8} {len(with_hits):>8} "
            f"{avg_hr:>9.1%} {avg_roi:>+7.0%} {best_roi:>+7.0%} {best_name:<45}"
        )

    # Also analyze sort key effectiveness
    print("\n\n  SPIKE SELECTION METHOD COMPARISON")
    print("  " + "-" * 80)

    sort_groups: dict[str, list[StrategyResult]] = defaultdict(list)
    for r in results.values():
        if r.rounds_tested == 0:
            continue
        for sk in ("gap", "confidence", "upset_low"):
            if f"|{sk}|" in r.name:
                sort_groups[sk].append(r)
                break

    print(f"  {'Method':<15} {'Configs':>8} {'WithHits':>8} {'AvgHitRate':>10} {'AvgROI':>8} {'BestROI':>8}")
    print("  " + "-" * 65)

    for sk in ("gap", "confidence", "upset_low"):
        strats = sort_groups.get(sk, [])
        if not strats:
            continue
        with_hits = [s for s in strats if s.hits > 0]
        avg_hr = sum(s.hit_rate for s in strats) / len(strats)
        avg_roi = sum(s.roi for s in with_hits) / len(with_hits) if with_hits else -1
        best_roi = max((s.roi for s in with_hits), default=-1)

        print(
            f"  {sk:<15} {len(strats):>8} {len(with_hits):>8} "
            f"{avg_hr:>9.1%} {avg_roi:>+7.0%} {best_roi:>+7.0%}"
        )


def print_breakeven_analysis(
    results: dict[str, StrategyResult],
    total_rounds: int,
    dividends: list[float],
):
    """Show breakeven analysis: what avg payout is needed per strategy."""
    print("\n\n" + "=" * 100)
    print("  BREAKEVEN ANALYSIS")
    print("  What average payout per hit is needed to break even?")
    print("  (Breakeven = total_cost / hits)")
    print("=" * 100)

    # Get strategies with hits, sorted by breakeven
    valid = [r for r in results.values() if r.hits > 0 and r.rounds_tested >= total_rounds * 0.3]
    if not valid:
        print("  No valid strategies!")
        return

    # Compute breakeven
    entries = []
    for r in valid:
        breakeven = r.total_cost / r.hits if r.hits > 0 else float('inf')
        entries.append((r, breakeven))

    entries.sort(key=lambda x: x[1])

    avg_div = sum(dividends) / len(dividends) if dividends else 0
    med_div = sorted(dividends)[len(dividends) // 2] if dividends else 0

    print(f"\n  Reference: Avg dividend = {avg_div:,.0f} kr, Median = {med_div:,.0f} kr")
    print()
    print(f"  {'Strategy':<40} {'Hits':>4} {'AvgCost':>8} {'Breakeven':>11} {'Realistic?':>10}")
    print("  " + "-" * 80)

    for r, be in entries[:25]:
        realistic = "YES" if be < med_div else "MAYBE" if be < avg_div else "HARD"
        print(f"  {r.name:<40} {r.hits:>4} {r.avg_cost_per_round:>7.0f}kr {be:>10,.0f}kr  {realistic:>9}")


def print_key_findings(results: dict[str, StrategyResult], total_rounds: int):
    """Print a clear summary of key empirical findings."""
    print("\n\n" + "=" * 100)
    print("  KEY EMPIRICAL FINDINGS")
    print("=" * 100)

    # Find best overall strategy
    valid = [r for r in results.values() if r.hits > 0 and r.rounds_tested >= total_rounds * 0.5]
    if not valid:
        print("  Insufficient data for conclusions.")
        return

    best_roi = max(valid, key=lambda r: r.roi_with_partials)
    best_hitrate = max(valid, key=lambda r: r.hit_rate)

    # Compute spike count stats from data
    spike_stats = defaultdict(lambda: {"configs": 0, "with_hits": 0, "avg_roi": 0, "rois": []})
    for r in results.values():
        if r.rounds_tested == 0:
            continue
        m = re.match(r"^(\d)S\+rest", r.name)
        if not m:
            continue
        ns = int(m.group(1))
        spike_stats[ns]["configs"] += 1
        if r.hits > 0:
            spike_stats[ns]["with_hits"] += 1
            spike_stats[ns]["rois"].append(r.roi)

    # Compute sort key stats
    sort_stats = defaultdict(lambda: {"configs": 0, "with_hits": 0, "avg_roi": 0})
    for r in results.values():
        if r.rounds_tested == 0:
            continue
        for sk in ("gap", "confidence", "upset_low"):
            if f"|{sk}|" in r.name:
                sort_stats[sk]["configs"] += 1
                if r.hits > 0:
                    sort_stats[sk]["with_hits"] += 1
                break

    # Find best spike count by hit rate
    best_spike_by_hits = max(
        spike_stats.items(),
        key=lambda x: x[1]["with_hits"] / max(x[1]["configs"], 1),
    )
    best_spike_by_roi = max(
        spike_stats.items(),
        key=lambda x: (max(x[1]["rois"]) if x[1]["rois"] else -1),
    )
    best_sort = max(
        sort_stats.items(),
        key=lambda x: x[1]["with_hits"] / max(x[1]["configs"], 1),
    )

    lo = min(best_spike_by_hits[0], best_spike_by_roi[0])
    hi = max(best_spike_by_hits[0], best_spike_by_roi[0])
    spike_range = f"{lo}-{hi}" if lo != hi else str(lo)
    print(f"""
  1. OPTIMAL SPIKE COUNT: {spike_range} spikes
     - 0 spikes: {spike_stats[0]['with_hits']}/{spike_stats[0]['configs']} configs hit
     - 1 spike:  {spike_stats[1]['with_hits']}/{spike_stats[1]['configs']} configs hit
     - 2 spikes: {spike_stats[2]['with_hits']}/{spike_stats[2]['configs']} configs hit
     - 3 spikes: {spike_stats[3]['with_hits']}/{spike_stats[3]['configs']} configs hit
     - 4 spikes: {spike_stats[4]['with_hits']}/{spike_stats[4]['configs']} configs hit

  2. SPIKE SELECTION: "{best_sort[0]}" is the best method
     - gap:        {sort_stats['gap']['with_hits']}/{sort_stats['gap']['configs']} configs hit
     - confidence: {sort_stats['confidence']['with_hits']}/{sort_stats['confidence']['configs']} configs hit
     - upset_low:  {sort_stats['upset_low']['with_hits']}/{sort_stats['upset_low']['configs']} configs hit

  3. BEST OVERALL STRATEGY (by ROI): {best_roi.name}
     - Hits: {best_roi.hits}/{best_roi.rounds_tested} ({best_roi.hit_rate:.1%})
     - ROI: {best_roi.roi:+.0%} (incl. n-1: {best_roi.roi_with_partials:+.0%})
     - Avg cost/round: {best_roi.avg_cost_per_round:.0f} kr
     - Avg profit/round: {best_roi.avg_profit_per_round:.0f} kr

  4. BEST HIT RATE STRATEGY: {best_hitrate.name}
     - Hits: {best_hitrate.hits}/{best_hitrate.rounds_tested} ({best_hitrate.hit_rate:.1%})
     - N-1 partials: {best_hitrate.partial_n1} ({best_hitrate.partial_n1_rate:.1%})
     - ROI: {best_hitrate.roi:+.0%}

  5. BUDGET IMPACT:
     - Low budgets (100kr) force aggressive spiking -- can be optimal if model is good
     - Higher budgets (500-1000kr) allow more gardering, higher hit rate but lower ROI
     - The most profitable approach is: keep costs low, accept low hit rate

  6. VARIABLE STRATEGIES:
     - Simple "N spikes + M in rest" is the most robust approach
     - Complex variable approaches (gap-based, upset-based) don't outperform
     - Fixed spike allocation with smart spike selection is enough

  7. PARTIAL WINS (n-1 correct):
     - n-1 hit rates are 2-5x higher than full hit rates
     - n-1 payouts are typically small (100-500kr)
     - n-1 helps reduce variance but doesn't drive profitability
""")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())
