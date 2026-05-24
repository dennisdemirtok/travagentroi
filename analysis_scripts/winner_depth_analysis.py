#!/usr/bin/env python3
"""Analyse how deep the winner falls in the model's ranking per race.

Goal: understand WHICH race features predict that the winner will be
ranked deep (e.g., rank 8+) vs near the top (rank 1-2).

This tells us how many picks each race ACTUALLY needs — not a fixed
number, but calibrated to the race's characteristics.

Output:
- Winner rank distribution overall
- Winner rank by race feature bins (upset_risk, gap, field size, etc.)
- A calibrated "required_picks" function
- Comparison: variable vs fixed-width system results
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
ROW_PRICE = 0.50


# ---------------------------------------------------------------------------
# Data loading (same as system_optimizer)
# ---------------------------------------------------------------------------

def find_round_ids(start_year: int = 2022) -> list[dict]:
    cal_re = re.compile(r"^(\d{4})-\d{2}-\d{2}_[a-f0-9]+\.json$")
    rounds = []
    for fp in sorted(glob.glob(str(CACHE_DIR / "*.json"))):
        fname = Path(fp).name
        m = cal_re.match(fname)
        if not m:
            continue
        if int(m.group(1)) < start_year:
            continue
        try:
            with open(fp) as f:
                data = json.load(f)
        except Exception:
            continue
        games = data.get("games", {})
        if not isinstance(games, dict):
            continue
        for gt in ("V75", "V85"):
            for g in games.get(gt, []):
                if g.get("status") == "results" and g.get("races"):
                    rounds.append({"game_type": gt, "game_id": g["id"]})
    return rounds


async def load_round(client, info):
    try:
        parts = info["game_id"].split("_")
        day = date.fromisoformat(parts[1])
        gr = await client.fetch_full_round(parts[0], day)
        if gr and gr.is_finished and gr.races:
            return gr
    except Exception:
        pass
    return None


def apply_temporal_filtering(gr):
    for race in gr.races:
        race_date = race.race_date
        for entry in race.entries:
            original = entry.horse.past_starts
            filtered = [s for s in original if s.start_date < race_date]
            if len(filtered) < len(original):
                entry.horse.past_starts = filtered
            entry.horse.recompute_career_from_starts()
            entry.bet_percentage = None
            entry.odds = None


# ---------------------------------------------------------------------------
# Per-race feature extraction
# ---------------------------------------------------------------------------

@dataclass
class RaceProfile:
    """Features and outcome for one historical race."""
    # Identification
    round_id: str = ""
    race_number: int = 0

    # Race features (known BEFORE the race)
    num_starters: int = 0
    is_volt: bool = False
    is_stolopp: bool = False
    has_tillagg: bool = False       # any horse has distance tillägg
    num_distances: int = 1          # number of distinct distances (spårtrappa)
    purse: int = 0

    # Model features (computed from analysis)
    upset_risk: float = 0.0
    gap_1_2: float = 0.0           # score gap rank 1 vs 2
    gap_1_3: float = 0.0           # score gap rank 1 vs 3
    gap_1_5: float = 0.0           # score gap rank 1 vs 5
    top3_spread: float = 0.0       # spread among top 3
    top5_spread: float = 0.0       # spread among top 5
    top1_score: float = 0.0
    field_std: float = 0.0         # std dev of all scores

    # Outcome (known AFTER the race)
    winner_model_rank: int = 0     # rank of actual winner in model ordering
    winner_post_position: int = 0

    # Dividend (for ROI calculation)
    top_dividend: float = 0.0
    n1_dividend: float = 0.0


def extract_race_profile(race: Race, round_id: str) -> RaceProfile | None:
    """Extract features + outcome for one race."""
    entries = sorted(race.active_entries, key=lambda e: e.super_score, reverse=True)
    if not entries or not race.result_order:
        return None

    winner_pp = race.result_order[0]
    scores = [e.super_score for e in entries]
    model_ranking = [e.post_position for e in entries]

    # Find winner's model rank
    winner_rank = -1
    for i, pp in enumerate(model_ranking):
        if pp == winner_pp:
            winner_rank = i + 1
            break
    if winner_rank < 0:
        return None

    # Race features
    rn = (race.race_name or "").lower()
    rt = (race.race_type or "").lower()
    is_stolopp = any(w in rn or w in rt for w in
                     ("stolopp", "storlopp", "stl", "final", "milen",
                      "derby", "grand", "elitlopp", "kriterium", "oaks"))

    distances = set(e.distance for e in entries if e.distance > 0)
    has_tillagg = any(e.distance > race.distance for e in entries if e.distance > 0 and race.distance > 0)

    # Score statistics
    gap_1_2 = scores[0] - scores[1] if len(scores) >= 2 else 0
    gap_1_3 = scores[0] - scores[2] if len(scores) >= 3 else 0
    gap_1_5 = scores[0] - scores[4] if len(scores) >= 5 else gap_1_3
    top3_spread = scores[0] - scores[2] if len(scores) >= 3 else 0
    top5_spread = scores[0] - scores[4] if len(scores) >= 5 else top3_spread

    # Standard deviation
    mean_s = sum(scores) / len(scores)
    field_std = math.sqrt(sum((s - mean_s) ** 2 for s in scores) / len(scores))

    return RaceProfile(
        round_id=round_id,
        race_number=race.race_number,
        num_starters=len(entries),
        is_volt=(race.start_method == StartMethod.VOLT),
        is_stolopp=is_stolopp,
        has_tillagg=has_tillagg,
        num_distances=len(distances),
        purse=race.purse,
        upset_risk=race.upset_risk,
        gap_1_2=gap_1_2,
        gap_1_3=gap_1_3,
        gap_1_5=gap_1_5,
        top3_spread=top3_spread,
        top5_spread=top5_spread,
        top1_score=scores[0],
        field_std=field_std,
        winner_model_rank=winner_rank,
        winner_post_position=winner_pp,
    )


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def print_winner_rank_distribution(profiles: list[RaceProfile]):
    """How often does model rank N win?"""
    print("\n" + "=" * 80)
    print("  WINNER MODEL RANK DISTRIBUTION")
    print("  (How often does the model's #1, #2, #3, ... win?)")
    print("=" * 80)

    total = len(profiles)
    rank_counts = defaultdict(int)
    for p in profiles:
        rank_counts[p.winner_model_rank] += 1

    # Cumulative: "if you pick top N, what % of winners covered?"
    print(f"\n  Total races analyzed: {total}\n")
    print(f"  {'Rank':>5} {'Count':>6} {'%':>6} {'Cumul':>7} {'Coverage if top-N':>20}")
    print("  " + "-" * 50)

    cumulative = 0
    for rank in range(1, max(rank_counts.keys()) + 1):
        count = rank_counts.get(rank, 0)
        cumulative += count
        pct = count / total * 100
        cum_pct = cumulative / total * 100
        bar = "█" * int(pct / 2)
        print(f"  {rank:>5} {count:>6} {pct:>5.1f}% {cum_pct:>6.1f}%  {bar}")

    # Key thresholds
    print(f"\n  Key coverage thresholds:")
    for n in [1, 2, 3, 4, 5, 6, 8, 10]:
        covered = sum(rank_counts.get(r, 0) for r in range(1, n + 1))
        print(f"    Top {n:>2}: {covered}/{total} = {covered / total:.1%}")


def print_rank_by_feature(profiles: list[RaceProfile]):
    """Winner rank broken down by race features."""
    print("\n\n" + "=" * 80)
    print("  WINNER RANK BY RACE FEATURES")
    print("  (Which features predict the winner being deep in the field?)")
    print("=" * 80)

    def analyze_group(name: str, groups: dict[str, list[RaceProfile]]):
        print(f"\n  --- {name} ---")
        print(f"  {'Group':<25} {'N':>5} {'AvgRank':>8} {'Med':>4} {'Top1':>5} {'Top3':>5} {'Top5':>5} {'6+':>5}")
        print("  " + "-" * 65)

        for label, profs in sorted(groups.items()):
            if not profs:
                continue
            n = len(profs)
            ranks = [p.winner_model_rank for p in profs]
            avg = sum(ranks) / n
            med = sorted(ranks)[n // 2]
            top1 = sum(1 for r in ranks if r == 1) / n
            top3 = sum(1 for r in ranks if r <= 3) / n
            top5 = sum(1 for r in ranks if r <= 5) / n
            deep = sum(1 for r in ranks if r >= 6) / n
            print(f"  {label:<25} {n:>5} {avg:>7.1f} {med:>4} {top1:>4.0%} {top3:>4.0%} {top5:>4.0%} {deep:>4.0%}")

    # By upset_risk bins
    bins = {"risk 0-15": [], "risk 15-30": [], "risk 30-45": [],
            "risk 45-60": [], "risk 60-75": [], "risk 75+": []}
    for p in profiles:
        if p.upset_risk < 15: bins["risk 0-15"].append(p)
        elif p.upset_risk < 30: bins["risk 15-30"].append(p)
        elif p.upset_risk < 45: bins["risk 30-45"].append(p)
        elif p.upset_risk < 60: bins["risk 45-60"].append(p)
        elif p.upset_risk < 75: bins["risk 60-75"].append(p)
        else: bins["risk 75+"].append(p)
    analyze_group("By Upset Risk", bins)

    # By gap 1→2
    bins = {"gap<5": [], "gap 5-10": [], "gap 10-15": [], "gap 15-20": [], "gap 20+": []}
    for p in profiles:
        if p.gap_1_2 < 5: bins["gap<5"].append(p)
        elif p.gap_1_2 < 10: bins["gap 5-10"].append(p)
        elif p.gap_1_2 < 15: bins["gap 10-15"].append(p)
        elif p.gap_1_2 < 20: bins["gap 15-20"].append(p)
        else: bins["gap 20+"].append(p)
    analyze_group("By Score Gap (rank 1 vs 2)", bins)

    # By field size
    bins = {"6-8 starters": [], "9-11 starters": [], "12-14 starters": [], "15+ starters": []}
    for p in profiles:
        if p.num_starters <= 8: bins["6-8 starters"].append(p)
        elif p.num_starters <= 11: bins["9-11 starters"].append(p)
        elif p.num_starters <= 14: bins["12-14 starters"].append(p)
        else: bins["15+ starters"].append(p)
    analyze_group("By Field Size", bins)

    # By start method
    bins = {"auto": [p for p in profiles if not p.is_volt],
            "volt": [p for p in profiles if p.is_volt]}
    analyze_group("By Start Method", bins)

    # By stolopp
    bins = {"normal race": [p for p in profiles if not p.is_stolopp],
            "stolopp/final": [p for p in profiles if p.is_stolopp]}
    analyze_group("By Race Type", bins)

    # By tillägg
    bins = {"no tillagg": [p for p in profiles if not p.has_tillagg],
            "has tillagg": [p for p in profiles if p.has_tillagg]}
    analyze_group("By Distance Tillägg", bins)

    # By top-5 spread (how differentiated is the model?)
    bins = {"tight (<15)": [], "moderate (15-25)": [], "spread (25-35)": [], "very spread (35+)": []}
    for p in profiles:
        if p.top5_spread < 15: bins["tight (<15)"].append(p)
        elif p.top5_spread < 25: bins["moderate (15-25)"].append(p)
        elif p.top5_spread < 35: bins["spread (25-35)"].append(p)
        else: bins["very spread (35+)"].append(p)
    analyze_group("By Top-5 Score Spread", bins)


def build_calibrated_picks(profiles: list[RaceProfile]) -> dict:
    """Build a calibrated picks function from historical data.

    For each combination of features, find the MINIMUM number of
    picks needed to cover the winner 85%+ of the time.

    Returns a lookup table: feature_bin → recommended_picks.
    """
    print("\n\n" + "=" * 80)
    print("  CALIBRATED PICKS TABLE")
    print("  (How many picks needed to cover winner X% of the time?)")
    print("=" * 80)

    # Multi-dimensional binning
    # Primary dimension: upset_risk (most predictive)
    # Secondary: gap_1_2, field_size

    bins = {
        "very_safe":    {"filter": lambda p: p.upset_risk < 15 and p.gap_1_2 >= 10, "profiles": []},
        "safe":         {"filter": lambda p: p.upset_risk < 25 and p.gap_1_2 >= 5, "profiles": []},
        "moderate_clear": {"filter": lambda p: p.upset_risk < 40 and p.gap_1_2 >= 8, "profiles": []},
        "moderate_tight": {"filter": lambda p: p.upset_risk < 40 and p.gap_1_2 < 8, "profiles": []},
        "risky_small":  {"filter": lambda p: p.upset_risk < 60 and p.num_starters <= 10, "profiles": []},
        "risky_large":  {"filter": lambda p: p.upset_risk < 60 and p.num_starters > 10, "profiles": []},
        "very_risky":   {"filter": lambda p: p.upset_risk >= 60 and p.num_starters <= 12, "profiles": []},
        "chaos":        {"filter": lambda p: p.upset_risk >= 60 and p.num_starters > 12, "profiles": []},
    }

    # Assign profiles to bins (first match wins — order matters)
    assigned = set()
    for p in profiles:
        for bin_name, bin_data in bins.items():
            if bin_data["filter"](p) and id(p) not in assigned:
                bin_data["profiles"].append(p)
                assigned.add(id(p))
                break

    # Unassigned go to a catch-all
    unassigned = [p for p in profiles if id(p) not in assigned]
    if unassigned:
        bins["unassigned"] = {"filter": None, "profiles": unassigned}

    print(f"\n  {'Bin':<20} {'N':>5} {'AvgRank':>8} {'picks@70%':>9} {'picks@80%':>9} {'picks@85%':>9} {'picks@90%':>9} {'picks@95%':>9}")
    print("  " + "-" * 82)

    calibration = {}
    for bin_name, bin_data in bins.items():
        profs = bin_data["profiles"]
        if not profs:
            continue
        n = len(profs)
        ranks = sorted([p.winner_model_rank for p in profs])
        avg_rank = sum(ranks) / n

        # Find picks needed for coverage thresholds
        picks_at = {}
        for threshold in [0.70, 0.80, 0.85, 0.90, 0.95]:
            target_idx = int(math.ceil(threshold * n)) - 1
            target_idx = min(target_idx, n - 1)
            picks_at[threshold] = ranks[target_idx]

        calibration[bin_name] = picks_at

        print(f"  {bin_name:<20} {n:>5} {avg_rank:>7.1f} "
              f"{picks_at[0.70]:>9} {picks_at[0.80]:>9} {picks_at[0.85]:>9} "
              f"{picks_at[0.90]:>9} {picks_at[0.95]:>9}")

    return calibration


def build_continuous_model(profiles: list[RaceProfile]):
    """Build a continuous model: features → predicted winner rank.

    Uses simple linear combination of features, fitted to minimize
    the predicted picks needed for ~85% coverage per feature bin.
    """
    print("\n\n" + "=" * 80)
    print("  CONTINUOUS DEPTH PREDICTION MODEL")
    print("  (Predict how many picks each race needs)")
    print("=" * 80)

    # Feature correlation with winner rank
    features = {
        "upset_risk":    [p.upset_risk for p in profiles],
        "gap_1_2":       [p.gap_1_2 for p in profiles],
        "gap_1_3":       [p.gap_1_3 for p in profiles],
        "gap_1_5":       [p.gap_1_5 for p in profiles],
        "top5_spread":   [p.top5_spread for p in profiles],
        "field_std":     [p.field_std for p in profiles],
        "num_starters":  [float(p.num_starters) for p in profiles],
        "is_volt":       [float(p.is_volt) for p in profiles],
        "is_stolopp":    [float(p.is_stolopp) for p in profiles],
        "has_tillagg":   [float(p.has_tillagg) for p in profiles],
        "num_distances": [float(p.num_distances) for p in profiles],
        "top1_score":    [p.top1_score for p in profiles],
    }

    targets = [p.winner_model_rank for p in profiles]
    n = len(profiles)
    mean_rank = sum(targets) / n

    print(f"\n  Feature correlations with winner rank (higher = deeper winner):")
    print(f"  Mean winner rank: {mean_rank:.2f}\n")

    print(f"  {'Feature':<18} {'Correlation':>12} {'Direction':>10}")
    print("  " + "-" * 42)

    correlations = {}
    for fname, fvals in features.items():
        # Pearson correlation
        mean_f = sum(fvals) / n
        mean_t = mean_rank
        cov = sum((f - mean_f) * (t - mean_t) for f, t in zip(fvals, targets)) / n
        std_f = math.sqrt(sum((f - mean_f) ** 2 for f in fvals) / n)
        std_t = math.sqrt(sum((t - mean_t) ** 2 for t in targets) / n)
        corr = cov / (std_f * std_t) if std_f > 0 and std_t > 0 else 0
        correlations[fname] = corr
        direction = "deeper ↑" if corr > 0.05 else "shallower ↓" if corr < -0.05 else "~neutral"
        print(f"  {fname:<18} {corr:>+11.3f} {direction:>10}")

    # Sort by absolute correlation
    sorted_corr = sorted(correlations.items(), key=lambda x: abs(x[1]), reverse=True)
    print(f"\n  Top predictors (by |correlation|):")
    for fname, corr in sorted_corr[:6]:
        print(f"    {fname}: r={corr:+.3f}")


def test_variable_systems(profiles: list[RaceProfile], calibration: dict,
                          all_rounds_data: list[tuple]) -> None:
    """Test variable-width systems against fixed-width baselines.

    all_rounds_data: list of (GameRound, dividends, n1_dividends)
    """
    print("\n\n" + "=" * 80)
    print("  VARIABLE vs FIXED WIDTH SYSTEM COMPARISON")
    print("  (Testing on full rounds — all legs must hit)")
    print("=" * 80)

    # Build the mapping function based on calibration
    # Use 80% coverage threshold as our picks target
    def calibrated_picks(race_profile: RaceProfile) -> int:
        """Predict picks needed for this race based on calibrated bins."""
        p = race_profile

        # Match to calibration bins (same logic as build_calibrated_picks)
        if p.upset_risk < 15 and p.gap_1_2 >= 10:
            bin_name = "very_safe"
        elif p.upset_risk < 25 and p.gap_1_2 >= 5:
            bin_name = "safe"
        elif p.upset_risk < 40 and p.gap_1_2 >= 8:
            bin_name = "moderate_clear"
        elif p.upset_risk < 40:
            bin_name = "moderate_tight"
        elif p.upset_risk < 60 and p.num_starters <= 10:
            bin_name = "risky_small"
        elif p.upset_risk < 60:
            bin_name = "risky_large"
        elif p.num_starters <= 12:
            bin_name = "very_risky"
        else:
            bin_name = "chaos"

        picks_table = calibration.get(bin_name, {})
        # Use 80% coverage as default target
        return picks_table.get(0.80, 4)

    def continuous_picks(race_profile: RaceProfile) -> int:
        """Continuous model: use features directly to estimate picks.

        Based on the strongest correlating features.
        """
        p = race_profile

        # Base: start from 2 picks
        picks = 2.0

        # Upset risk is the strongest predictor
        if p.upset_risk < 15:
            picks += 0.0
        elif p.upset_risk < 30:
            picks += 0.5
        elif p.upset_risk < 45:
            picks += 1.5
        elif p.upset_risk < 60:
            picks += 2.5
        elif p.upset_risk < 75:
            picks += 3.5
        else:
            picks += 5.0

        # Gap 1→2: smaller gap = need more picks
        if p.gap_1_2 < 3:
            picks += 2.0
        elif p.gap_1_2 < 6:
            picks += 1.0
        elif p.gap_1_2 < 10:
            picks += 0.5
        elif p.gap_1_2 < 15:
            picks += 0.0
        else:
            picks -= 0.5

        # Field size
        if p.num_starters >= 14:
            picks += 1.5
        elif p.num_starters >= 12:
            picks += 0.5
        elif p.num_starters <= 8:
            picks -= 0.5

        # Volt adds uncertainty
        if p.is_volt:
            picks += 0.5

        # Stolopp adds upset chance
        if p.is_stolopp:
            picks += 1.0

        # Top5 spread: if tight, need more picks
        if p.top5_spread < 10:
            picks += 1.5
        elif p.top5_spread < 20:
            picks += 0.5

        return max(1, min(12, round(picks)))

    # Test strategies on full rounds
    strategies = {
        "fixed_3": lambda p: 3,
        "fixed_4": lambda p: 4,
        "fixed_5": lambda p: 5,
        "fixed_6": lambda p: 6,
        "calibrated_80": calibrated_picks,
        "continuous": continuous_picks,
    }

    budgets = [100, 200, 500, 1000]

    for budget in budgets:
        print(f"\n\n  ══════ Budget: {budget} kr ══════")
        print(f"  {'Strategy':<20} {'Rounds':>6} {'Hits':>4} {'N-1':>4} {'HitRate':>7} "
              f"{'AvgCost':>8} {'ROI':>8} {'AvgPicks':>9}")
        print("  " + "-" * 75)

        for strat_name, picks_fn in strategies.items():
            hits = 0
            n1_hits = 0
            total_cost = 0.0
            total_payout = 0.0
            tested = 0
            all_picks_list = []

            for gr, race_profiles, dividend, n1_div in all_rounds_data:
                num_legs = len(race_profiles)

                # Assign picks per leg
                picks_per_leg = []
                for rp in race_profiles:
                    picks_per_leg.append(picks_fn(rp))

                # Calculate cost
                rows = 1
                for p in picks_per_leg:
                    rows *= p
                cost = rows * ROW_PRICE

                # Budget constraint: scale down if over budget
                if cost > budget:
                    # Find and reduce widest legs
                    while cost > budget:
                        widest_idx = max(range(len(picks_per_leg)),
                                         key=lambda i: picks_per_leg[i])
                        if picks_per_leg[widest_idx] <= 1:
                            break
                        picks_per_leg[widest_idx] -= 1
                        rows = 1
                        for p in picks_per_leg:
                            rows *= p
                        cost = rows * ROW_PRICE

                tested += 1
                total_cost += cost
                all_picks_list.extend(picks_per_leg)

                # Check hits
                correct = 0
                for rp, n_picks in zip(race_profiles, picks_per_leg):
                    if rp.winner_model_rank <= n_picks:
                        correct += 1

                if correct == num_legs:
                    hits += 1
                    total_payout += dividend
                elif correct == num_legs - 1:
                    n1_hits += 1

            if tested > 0:
                hitrate = hits / tested
                roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
                avg_cost = total_cost / tested
                avg_picks = sum(all_picks_list) / len(all_picks_list) if all_picks_list else 0

                print(f"  {strat_name:<20} {tested:>6} {hits:>4} {n1_hits:>4} "
                      f"{hitrate:>6.1%} {avg_cost:>7.0f}kr {roi:>+7.0%} {avg_picks:>8.1f}")


def print_optimal_variable_formula(profiles: list[RaceProfile]):
    """Output the final recommended variable picks formula."""
    print("\n\n" + "=" * 80)
    print("  RECOMMENDED VARIABLE PICKS FORMULA")
    print("  (For integration into system_builder.py)")
    print("=" * 80)

    # Group profiles by predicted difficulty and check coverage
    def compute_needed_picks(profs: list[RaceProfile], coverage: float = 0.80) -> int:
        if not profs:
            return 3
        ranks = sorted([p.winner_model_rank for p in profs])
        idx = min(int(math.ceil(coverage * len(ranks))) - 1, len(ranks) - 1)
        return ranks[idx]

    # Build the formula bins
    bins = [
        ("SPIKE (1 pick)",     lambda p: p.upset_risk < 15 and p.gap_1_2 >= 15),
        ("TIGHT (2 picks)",    lambda p: p.upset_risk < 20 and p.gap_1_2 >= 8),
        ("SAFE (2-3 picks)",   lambda p: p.upset_risk < 30 and p.gap_1_2 >= 5),
        ("MODERATE (3-4)",     lambda p: p.upset_risk < 45 and p.gap_1_2 >= 3),
        ("OPEN (4-6)",         lambda p: p.upset_risk < 60),
        ("RISKY (5-8)",        lambda p: p.upset_risk < 75),
        ("CHAOS (7-10+)",      lambda p: True),
    ]

    print(f"\n  {'Category':<22} {'N':>5} {'AvgRank':>8} {'@70%':>5} {'@80%':>5} {'@85%':>5} {'@90%':>5}")
    print("  " + "-" * 58)

    assigned = set()
    for label, filter_fn in bins:
        profs = [p for p in profiles if filter_fn(p) and id(p) not in assigned]
        for p in profs:
            assigned.add(id(p))

        if not profs:
            continue

        ranks = sorted([p.winner_model_rank for p in profs])
        n = len(profs)
        avg = sum(ranks) / n

        p70 = ranks[min(int(math.ceil(0.70 * n)) - 1, n - 1)]
        p80 = ranks[min(int(math.ceil(0.80 * n)) - 1, n - 1)]
        p85 = ranks[min(int(math.ceil(0.85 * n)) - 1, n - 1)]
        p90 = ranks[min(int(math.ceil(0.90 * n)) - 1, n - 1)]

        print(f"  {label:<22} {n:>5} {avg:>7.1f} {p70:>5} {p80:>5} {p85:>5} {p90:>5}")

    print("""
  FORMULA for system_builder.py:

  def predict_required_picks(upset_risk, gap_1_2, num_starters, is_volt, is_stolopp, top5_spread):
      # Base picks from upset risk
      if upset_risk < 15:  base = 1
      elif upset_risk < 25: base = 2
      elif upset_risk < 40: base = 3
      elif upset_risk < 55: base = 4
      elif upset_risk < 70: base = 5
      else: base = 7

      # Gap adjustment
      if gap_1_2 >= 15:    base -= 1
      elif gap_1_2 >= 10:  base -= 0.5
      elif gap_1_2 < 5:    base += 1
      elif gap_1_2 < 3:    base += 2

      # Field size
      if num_starters >= 14: base += 1
      elif num_starters >= 12: base += 0.5

      # Start method & type
      if is_volt: base += 0.5
      if is_stolopp: base += 1

      # Top5 spread (tight field = harder to differentiate)
      if top5_spread < 10: base += 1
      elif top5_spread < 15: base += 0.5

      return max(1, min(num_starters, round(base)))
  """)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    t0 = time.time()
    client = ATGClient()
    analyzer = CompositeAnalyzer(DEFAULT_CONFIG)

    print("=" * 80)
    print("  WINNER DEPTH ANALYSIS")
    print("  Understanding how deep winners fall in the model's ranking")
    print("=" * 80)

    # Load rounds
    round_infos = find_round_ids()
    random.shuffle(round_infos)
    round_infos = round_infos[:200]
    print(f"\nLoading up to {len(round_infos)} rounds...")

    all_profiles: list[RaceProfile] = []
    all_rounds_data = []  # For system testing

    loaded = 0
    for info in round_infos:
        gr = await load_round(client, info)
        if not gr:
            continue

        has_results = all(r.result_order for r in gr.races)
        if not has_results:
            continue

        num_legs = len(gr.races)
        top_div = gr.dividends.get(num_legs, 0.0)
        n1_div = gr.dividends.get(num_legs - 1, 0.0)
        if top_div <= 0:
            continue

        apply_temporal_filtering(gr)
        analyzer.analyze_round(gr)

        round_profiles = []
        for race in gr.races:
            profile = extract_race_profile(race, info["game_id"])
            if profile:
                all_profiles.append(profile)
                round_profiles.append(profile)

        if len(round_profiles) == num_legs:
            all_rounds_data.append((gr, round_profiles, top_div, n1_div))

        loaded += 1
        if loaded % 20 == 0:
            print(f"  Loaded {loaded} rounds ({time.time() - t0:.0f}s)...")

    print(f"\nLoaded {loaded} rounds, {len(all_profiles)} individual races")
    print(f"Complete rounds for system testing: {len(all_rounds_data)}")

    if len(all_profiles) < 50:
        print("Too few races, aborting.")
        return

    # 1. Overall winner rank distribution
    print_winner_rank_distribution(all_profiles)

    # 2. Winner rank by features
    print_rank_by_feature(all_profiles)

    # 3. Continuous model (correlations)
    build_continuous_model(all_profiles)

    # 4. Calibrated picks table
    calibration = build_calibrated_picks(all_profiles)

    # 5. Test variable vs fixed systems
    test_variable_systems(all_profiles, calibration, all_rounds_data)

    # 6. Recommended formula
    print_optimal_variable_formula(all_profiles)

    elapsed = time.time() - t0
    print(f"\nTotal runtime: {elapsed:.0f}s")

    # Save raw data for further analysis
    output = {
        "total_races": len(all_profiles),
        "total_rounds": len(all_rounds_data),
        "profiles": [
            {
                "round_id": p.round_id,
                "race_number": p.race_number,
                "upset_risk": p.upset_risk,
                "gap_1_2": p.gap_1_2,
                "gap_1_3": p.gap_1_3,
                "gap_1_5": p.gap_1_5,
                "top5_spread": p.top5_spread,
                "num_starters": p.num_starters,
                "is_volt": p.is_volt,
                "is_stolopp": p.is_stolopp,
                "has_tillagg": p.has_tillagg,
                "field_std": p.field_std,
                "winner_model_rank": p.winner_model_rank,
            }
            for p in all_profiles
        ]
    }
    with open("winner_depth_data.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"Saved raw data to winner_depth_data.json")


if __name__ == "__main__":
    asyncio.run(main())
