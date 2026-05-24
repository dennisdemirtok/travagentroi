#!/usr/bin/env python3
"""Deep strategy optimizer — probability-maximizing pick allocation.

Instead of fixed "2 spikes + rest 4", this finds the MATHEMATICALLY
optimal pick allocation per race, per round.

Approach:
1. Build empirical coverage curves by difficulty level
2. For each round × budget: greedy-allocate picks to maximize P(all correct)
3. Compare to fixed strategies
4. Test round selection (skip bad rounds)
5. Dividend-weighted expected value
6. Bootstrap confidence intervals

Dennis: "i vissa lopp behövs bredare och vissa lopp kortare"
"""

import asyncio
import json
import glob
import re
import sys
import math
import random
import time
from datetime import date
from pathlib import Path
from collections import defaultdict
import statistics

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.data.models import GameRound, Race, StartMethod
from trav_agent.analysis.composite import CompositeAnalyzer
from trav_agent.analysis.system_builder import predict_difficulty
from trav_agent.config import DEFAULT_CONFIG

random.seed(42)

CACHE_DIR = Path(__file__).parent / "cache"
ROW_PRICE = 0.50


# ═══════════════════════════════════════════════════════════════════════════
# Data loading (reused from previous scripts)
# ═══════════════════════════════════════════════════════════════════════════

def find_round_ids(start_year=2022):
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
        for gt in ("V75", "V86", "V85", "V64"):
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


# ═══════════════════════════════════════════════════════════════════════════
# Empirical coverage curves
# ═══════════════════════════════════════════════════════════════════════════

def build_coverage_curves(rounds_data):
    """Build P(winner in top-k | difficulty) from historical data."""
    # Collect (difficulty, winner_rank) pairs
    observations = []
    for gr, _, _ in rounds_data:
        for race in gr.races:
            if not race.result_order:
                continue
            winner_pp = race.result_order[0]
            sorted_entries = sorted(
                race.active_entries,
                key=lambda e: e.super_score, reverse=True
            )
            rank = next(
                (i + 1 for i, e in enumerate(sorted_entries)
                 if e.post_position == winner_pp),
                len(sorted_entries)
            )
            diff = predict_difficulty(race)
            n_starters = len(race.active_entries)
            observations.append({
                "difficulty": diff,
                "winner_rank": rank,
                "n_starters": n_starters,
            })

    # Build curves using continuous difficulty
    # Sort into quintile buckets
    diffs = [o["difficulty"] for o in observations]
    if len(diffs) < 20:
        return None

    # 5 buckets
    sorted_diffs = sorted(diffs)
    n = len(sorted_diffs)
    boundaries = [
        sorted_diffs[n // 5],
        sorted_diffs[2 * n // 5],
        sorted_diffs[3 * n // 5],
        sorted_diffs[4 * n // 5],
    ]

    def get_bucket(d):
        if d < boundaries[0]: return 0
        if d < boundaries[1]: return 1
        if d < boundaries[2]: return 2
        if d < boundaries[3]: return 3
        return 4

    bucket_names = ["easiest", "easy", "medium", "hard", "hardest"]

    # For each bucket, compute P(rank <= k) for k = 1..15
    curves = {}
    for b in range(5):
        bucket_obs = [o for o in observations if get_bucket(o["difficulty"]) == b]
        total = len(bucket_obs)
        if total == 0:
            continue
        probs = {}
        for k in range(1, 16):
            probs[k] = sum(1 for o in bucket_obs if o["winner_rank"] <= k) / total
        curves[b] = {
            "name": bucket_names[b],
            "count": total,
            "probs": probs,
            "avg_difficulty": sum(o["difficulty"] for o in bucket_obs) / total,
            "diff_range": (
                min(o["difficulty"] for o in bucket_obs),
                max(o["difficulty"] for o in bucket_obs),
            ),
        }

    return curves, boundaries, observations


def get_coverage_prob(difficulty, picks, curves, boundaries):
    """Interpolate coverage probability for a given difficulty and pick count."""
    # Find bucket
    if difficulty < boundaries[0]:
        b = 0
    elif difficulty < boundaries[1]:
        b = 1
    elif difficulty < boundaries[2]:
        b = 2
    elif difficulty < boundaries[3]:
        b = 3
    else:
        b = 4

    picks = min(picks, 15)
    picks = max(picks, 1)
    return curves[b]["probs"].get(picks, 0.95)


# ═══════════════════════════════════════════════════════════════════════════
# Greedy probability-maximizing allocation
# ═══════════════════════════════════════════════════════════════════════════

def greedy_allocate(race_diffs, max_rows, curves, boundaries):
    """Allocate picks to maximize P(all legs correct) within row budget.

    Greedy: start at 1 pick per leg, add picks one at a time to the leg
    where the gain in log-probability per unit of log-cost is highest.

    race_diffs: list of (race_number, difficulty, n_starters)
    max_rows: maximum total rows allowed
    """
    n = len(race_diffs)
    picks = [1] * n

    # Current total rows
    def total_rows():
        r = 1
        for p in picks:
            r *= p
        return r

    def total_log_prob():
        lp = 0
        for i, (rn, diff, ns) in enumerate(race_diffs):
            p = get_coverage_prob(diff, picks[i], curves, boundaries)
            lp += math.log(max(p, 1e-10))
        return lp

    # Greedy expansion
    for _ in range(100):
        if total_rows() >= max_rows:
            break

        best_gain = -float("inf")
        best_idx = -1

        for i, (rn, diff, ns) in enumerate(race_diffs):
            if picks[i] >= ns:  # Can't pick more than starters
                continue
            new_picks = picks[i] + 1
            new_rows = total_rows() * new_picks // picks[i]
            if new_rows > max_rows:
                continue

            # Gain in log-probability
            old_p = get_coverage_prob(diff, picks[i], curves, boundaries)
            new_p = get_coverage_prob(diff, new_picks, curves, boundaries)
            gain = math.log(max(new_p, 1e-10)) - math.log(max(old_p, 1e-10))

            # Cost in log-rows
            cost = math.log(new_picks) - math.log(picks[i])

            # Value per cost unit
            ratio = gain / max(cost, 1e-10)

            if ratio > best_gain:
                best_gain = ratio
                best_idx = i

        if best_idx < 0:
            break

        picks[best_idx] += 1

    return picks


def greedy_allocate_v2(race_diffs, max_rows, curves, boundaries):
    """V2: Pure probability maximization with budget constraint.

    Start at 1 per leg. Each step, add 1 pick to the leg where the
    absolute probability gain is highest, provided we stay within budget.
    """
    n = len(race_diffs)
    picks = [1] * n

    def total_rows():
        r = 1
        for p in picks:
            r *= p
        return r

    for _ in range(200):
        if total_rows() >= max_rows:
            break

        best_gain = -float("inf")
        best_idx = -1

        for i, (rn, diff, ns) in enumerate(race_diffs):
            if picks[i] >= ns:
                continue
            new_rows = total_rows() * (picks[i] + 1) // picks[i]
            if new_rows > max_rows:
                continue

            old_p = get_coverage_prob(diff, picks[i], curves, boundaries)
            new_p = get_coverage_prob(diff, picks[i] + 1, curves, boundaries)
            gain = new_p - old_p  # Absolute probability gain

            if gain > best_gain:
                best_gain = gain
                best_idx = i

        if best_idx < 0 or best_gain <= 0:
            break

        picks[best_idx] += 1

    return picks


def greedy_allocate_v3(race_diffs, max_rows, curves, boundaries):
    """V3: Maximize product of probabilities (= P(all correct)).

    Each step: add 1 pick to the leg where P_new/P_old is highest,
    i.e., the multiplicative improvement is biggest.
    """
    n = len(race_diffs)
    picks = [1] * n

    def total_rows():
        r = 1
        for p in picks:
            r *= p
        return r

    for _ in range(200):
        if total_rows() >= max_rows:
            break

        best_ratio = 1.0
        best_idx = -1

        for i, (rn, diff, ns) in enumerate(race_diffs):
            if picks[i] >= ns:
                continue
            new_rows = total_rows() * (picks[i] + 1) // picks[i]
            if new_rows > max_rows:
                continue

            old_p = get_coverage_prob(diff, picks[i], curves, boundaries)
            new_p = get_coverage_prob(diff, picks[i] + 1, curves, boundaries)
            ratio = new_p / max(old_p, 1e-10)

            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i

        if best_idx < 0 or best_ratio <= 1.0:
            break

        picks[best_idx] += 1

    return picks


# ═══════════════════════════════════════════════════════════════════════════
# Fixed strategy baseline
# ═══════════════════════════════════════════════════════════════════════════

def fixed_strategy(race_diffs, n_spikes, rest_width, max_rows):
    """Fixed: N lowest-difficulty races get 1 pick, rest get rest_width."""
    n = len(race_diffs)
    # Sort by difficulty (easiest first)
    indexed = sorted(enumerate(race_diffs), key=lambda x: x[1][1])

    picks = [0] * n
    for rank, (orig_idx, (rn, diff, ns)) in enumerate(indexed):
        if rank < n_spikes:
            picks[orig_idx] = 1
        else:
            picks[orig_idx] = min(rest_width, ns)

    # Trim to budget
    rows = 1
    for p in picks:
        rows *= p

    while rows > max_rows:
        # Reduce widest non-spike
        widest_idx = -1
        widest_val = 0
        for i in range(n):
            if picks[i] > 1 and picks[i] > widest_val:
                widest_val = picks[i]
                widest_idx = i
        if widest_idx < 0:
            break
        picks[widest_idx] -= 1
        rows = 1
        for p in picks:
            rows *= p

    return picks


# ═══════════════════════════════════════════════════════════════════════════
# Evaluation
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_system(gr, picks_per_race, race_numbers):
    """Check if system hits: winner must be in top-k for each leg."""
    correct = 0
    total = len(gr.races)

    for race in gr.races:
        if not race.result_order:
            continue
        winner_pp = race.result_order[0]
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score, reverse=True
        )

        idx = next(
            (i for i, rn in enumerate(race_numbers) if rn == race.race_number),
            None
        )
        if idx is None:
            continue

        n_picks = picks_per_race[idx]
        top_picks = [e.post_position for e in sorted_entries[:n_picks]]

        if winner_pp in top_picks:
            correct += 1

    return correct, total


# ═══════════════════════════════════════════════════════════════════════════
# Round difficulty scoring (for round selection)
# ═══════════════════════════════════════════════════════════════════════════

def round_difficulty_score(gr):
    """Overall round difficulty — lower = easier to hit."""
    diffs = [predict_difficulty(race) for race in gr.races]
    return sum(diffs) / len(diffs) if diffs else 50.0


def round_predicted_hitprob(race_diffs, picks, curves, boundaries):
    """Estimated probability of hitting all legs."""
    p = 1.0
    for i, (rn, diff, ns) in enumerate(race_diffs):
        p *= get_coverage_prob(diff, picks[i], curves, boundaries)
    return p


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    t0 = time.time()
    client = ATGClient()
    analyzer = CompositeAnalyzer(DEFAULT_CONFIG)

    print("=" * 100)
    print("  DEEP STRATEGY OPTIMIZER")
    print("  Probability-maximizing allocation vs fixed strategies")
    print("=" * 100)

    round_infos = find_round_ids()
    random.shuffle(round_infos)
    round_infos = round_infos[:300]

    # Load rounds
    rounds_data = []
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
        if top_div <= 0:
            continue

        apply_temporal_filtering(gr)
        analyzer.analyze_round(gr)
        rounds_data.append((gr, top_div, gr.dividends.get(num_legs - 1, 0.0)))
        loaded += 1
        if loaded % 25 == 0:
            print(f"  Loaded {loaded} rounds ({time.time() - t0:.0f}s)...")

    print(f"\nLoaded {loaded} rounds")

    # ── Build coverage curves from the data ─────────────────────────
    result = build_coverage_curves(rounds_data)
    if not result:
        print("Not enough data!")
        return
    curves, boundaries, observations = result

    print(f"\n{'─' * 80}")
    print("  COVERAGE CURVES (P(winner in top-k) by difficulty quintile)")
    print(f"{'─' * 80}")
    header = f"{'Picks':>5}"
    for b in range(5):
        header += f"  {curves[b]['name']:>10} ({curves[b]['count']:>3})"
    print(header)
    for k in range(1, 11):
        row = f"{k:>5}"
        for b in range(5):
            row += f"  {curves[b]['probs'][k]:>14.1%}"
        print(row)

    # ── Test strategies at multiple budgets ──────────────────────────
    budgets = [300, 500, 750, 1000, 1500]

    for budget in budgets:
        max_rows = int(budget / ROW_PRICE)

        print(f"\n{'═' * 100}")
        print(f"  BUDGET: {budget} kr ({max_rows} rader)")
        print(f"{'═' * 100}")

        strategies = {}

        # Greedy strategies
        for name, allocator in [
            ("greedy_ratio", greedy_allocate),
            ("greedy_abs", greedy_allocate_v2),
            ("greedy_mult", greedy_allocate_v3),
        ]:
            strategies[name] = {
                "hits": 0, "n1": 0, "n2": 0,
                "tested": 0, "total_cost": 0, "total_payout": 0,
                "pick_dist": defaultdict(int),
                "pred_probs": [],
                "per_leg_correct": [],
            }

        # Fixed strategies
        for ns in [0, 1, 2, 3, 4]:
            for rw in [3, 4, 5, 6]:
                name = f"fixed_{ns}S+rest{rw}"
                strategies[name] = {
                    "hits": 0, "n1": 0, "n2": 0,
                    "tested": 0, "total_cost": 0, "total_payout": 0,
                    "pick_dist": defaultdict(int),
                    "pred_probs": [],
                    "per_leg_correct": [],
                }

        for gr, dividend, n1_div in rounds_data:
            num_legs = len(gr.races)

            # Build race info
            race_diffs = []
            race_numbers = []
            for race in gr.races:
                diff = predict_difficulty(race)
                ns = len(race.active_entries)
                race_diffs.append((race.race_number, diff, ns))
                race_numbers.append(race.race_number)

            # Sort by difficulty for greedy
            sorted_race_diffs = sorted(race_diffs, key=lambda x: x[1])

            # Test each strategy
            for strat_name, allocator in [
                ("greedy_ratio", greedy_allocate),
                ("greedy_abs", greedy_allocate_v2),
                ("greedy_mult", greedy_allocate_v3),
            ]:
                picks = allocator(race_diffs, max_rows, curves, boundaries)
                correct, total = evaluate_system(gr, picks, race_numbers)
                rows = 1
                for p in picks:
                    rows *= p
                cost = rows * ROW_PRICE

                s = strategies[strat_name]
                s["tested"] += 1
                s["total_cost"] += cost
                for p in picks:
                    s["pick_dist"][p] += 1

                pred_p = round_predicted_hitprob(race_diffs, picks, curves, boundaries)
                s["pred_probs"].append(pred_p)
                s["per_leg_correct"].append(correct / total if total > 0 else 0)

                if correct == total:
                    s["hits"] += 1
                    s["total_payout"] += dividend
                elif correct == total - 1:
                    s["n1"] += 1
                elif correct == total - 2:
                    s["n2"] += 1

            # Fixed strategies
            for ns in [0, 1, 2, 3, 4]:
                for rw in [3, 4, 5, 6]:
                    name = f"fixed_{ns}S+rest{rw}"
                    picks = fixed_strategy(race_diffs, ns, rw, max_rows)
                    correct, total = evaluate_system(gr, picks, race_numbers)
                    rows = 1
                    for p in picks:
                        rows *= p
                    cost = rows * ROW_PRICE

                    s = strategies[name]
                    s["tested"] += 1
                    s["total_cost"] += cost

                    pred_p = round_predicted_hitprob(race_diffs, picks, curves, boundaries)
                    s["pred_probs"].append(pred_p)
                    s["per_leg_correct"].append(correct / total if total > 0 else 0)

                    if correct == total:
                        s["hits"] += 1
                        s["total_payout"] += dividend
                    elif correct == total - 1:
                        s["n1"] += 1
                    elif correct == total - 2:
                        s["n2"] += 1

        # Print results
        print(f"\n  {'Strategy':<20} {'Hits':>4} {'N-1':>4} {'N-2':>4} "
              f"{'HitRate':>7} {'AvgCost':>8} {'ROI':>8} {'AvgPredP':>9} {'AvgLegHit':>10}")
        print("  " + "─" * 90)

        sorted_strats = sorted(
            strategies.items(),
            key=lambda x: (x[1]["hits"], x[1]["total_payout"] - x[1]["total_cost"]),
            reverse=True,
        )

        for name, s in sorted_strats[:25]:
            if s["tested"] == 0:
                continue
            hitrate = s["hits"] / s["tested"]
            avg_cost = s["total_cost"] / s["tested"]
            roi = (s["total_payout"] - s["total_cost"]) / s["total_cost"] if s["total_cost"] > 0 else 0
            avg_pred = statistics.mean(s["pred_probs"]) if s["pred_probs"] else 0
            avg_leg = statistics.mean(s["per_leg_correct"]) if s["per_leg_correct"] else 0

            marker = " ◀" if name.startswith("greedy") else ""
            print(f"  {name:<20} {s['hits']:>4} {s['n1']:>4} {s['n2']:>4} "
                  f"{hitrate:>6.1%} {avg_cost:>7.0f}kr {roi:>+7.0%} {avg_pred:>8.2%} {avg_leg:>9.1%}{marker}")

        # Pick distribution for greedy strategies
        for strat_name in ["greedy_ratio", "greedy_abs", "greedy_mult"]:
            s = strategies[strat_name]
            if not s["pick_dist"]:
                continue
            total_picks = sum(s["pick_dist"].values())
            dist_str = ", ".join(
                f"{k}={v} ({v/total_picks:.0%})"
                for k, v in sorted(s["pick_dist"].items())
            )
            print(f"  {strat_name} picks: {dist_str}")

    # ═══════════════════════════════════════════════════════════════
    # ROUND SELECTION ANALYSIS
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  ROUND SELECTION: Spelar vi alla omgangar eller valjer vi?")
    print(f"{'═' * 100}")

    budget = 500
    max_rows = int(budget / ROW_PRICE)

    # For each round, compute predicted hit probability
    round_analysis = []
    for gr, dividend, n1_div in rounds_data:
        race_diffs = []
        race_numbers = []
        for race in gr.races:
            diff = predict_difficulty(race)
            ns = len(race.active_entries)
            race_diffs.append((race.race_number, diff, ns))
            race_numbers.append(race.race_number)

        picks = greedy_allocate(race_diffs, max_rows, curves, boundaries)
        correct, total = evaluate_system(gr, picks, race_numbers)
        rows = 1
        for p in picks:
            rows *= p
        cost = rows * ROW_PRICE

        pred_p = round_predicted_hitprob(race_diffs, picks, curves, boundaries)
        round_diff = round_difficulty_score(gr)

        round_analysis.append({
            "game_type": gr.game_type,
            "round_date": str(gr.round_date),
            "round_difficulty": round_diff,
            "pred_prob": pred_p,
            "actual_hit": correct == total,
            "n1": correct == total - 1,
            "correct": correct,
            "total": total,
            "cost": cost,
            "dividend": dividend,
            "picks": picks,
        })

    # Sort by predicted probability
    round_analysis.sort(key=lambda x: x["pred_prob"], reverse=True)

    # Test: what if we only play the top-N% easiest rounds?
    print(f"\n  Play top-N% by predicted probability (budget {budget}kr):")
    print(f"  {'Top%':>5} {'Played':>6} {'Hits':>4} {'N-1':>4} {'HitRate':>7} "
          f"{'TotalCost':>10} {'TotalPay':>10} {'ROI':>8} {'Profit':>10}")
    print("  " + "─" * 80)

    for pct in [100, 75, 50, 40, 30, 25, 20, 15, 10]:
        n_play = max(1, int(len(round_analysis) * pct / 100))
        subset = round_analysis[:n_play]
        hits = sum(1 for r in subset if r["actual_hit"])
        n1 = sum(1 for r in subset if r["n1"])
        total_cost = sum(r["cost"] for r in subset)
        total_payout = sum(r["dividend"] for r in subset if r["actual_hit"])
        hitrate = hits / n_play if n_play > 0 else 0
        roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0
        profit = total_payout - total_cost
        print(f"  {pct:>4}% {n_play:>6} {hits:>4} {n1:>4} {hitrate:>6.1%} "
              f"{total_cost:>9.0f}kr {total_payout:>9.0f}kr {roi:>+7.0%} {profit:>+9.0f}kr")

    # ═══════════════════════════════════════════════════════════════
    # DIVIDEND ANALYSIS: Which dividends matter?
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  DIVIDEND DISTRIBUTION & TARGET ANALYSIS")
    print(f"{'═' * 100}")

    dividends = sorted([r["dividend"] for r in round_analysis])

    # Dividend buckets
    buckets = [
        (0, 10000, "<10k"),
        (10000, 50000, "10k-50k"),
        (50000, 100000, "50k-100k"),
        (100000, 500000, "100k-500k"),
        (500000, 1000000, "500k-1M"),
        (1000000, float("inf"), ">1M"),
    ]

    print(f"\n  {'Dividend':>12} {'Count':>6} {'%':>5} {'Hits':>4} {'HitRate':>7} {'AvgDiff':>8}")
    print("  " + "─" * 50)
    for lo, hi, label in buckets:
        subset = [r for r in round_analysis if lo <= r["dividend"] < hi]
        if not subset:
            continue
        hits = sum(1 for r in subset if r["actual_hit"])
        avg_diff = statistics.mean(r["round_difficulty"] for r in subset)
        hitrate = hits / len(subset) if subset else 0
        print(f"  {label:>12} {len(subset):>6} {len(subset)/len(round_analysis):>4.0%} "
              f"{hits:>4} {hitrate:>6.1%} {avg_diff:>7.1f}")

    # Are higher dividends correlated with round difficulty?
    print(f"\n  Correlation: dividend vs round difficulty")
    divs = [r["dividend"] for r in round_analysis]
    diffs = [r["round_difficulty"] for r in round_analysis]
    n = len(divs)
    mean_d = sum(divs) / n
    mean_diff = sum(diffs) / n
    cov = sum((divs[i] - mean_d) * (diffs[i] - mean_diff) for i in range(n)) / n
    std_d = math.sqrt(sum((d - mean_d) ** 2 for d in divs) / n)
    std_diff = math.sqrt(sum((d - mean_diff) ** 2 for d in diffs) / n)
    corr = cov / (std_d * std_diff) if std_d * std_diff > 0 else 0
    print(f"  r = {corr:.3f}")

    # ═══════════════════════════════════════════════════════════════
    # PER-LEG ANALYSIS: Which legs cost us?
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  PER-LEG ANALYSIS: Coverage by difficulty quintile")
    print(f"{'═' * 100}")

    budget = 500
    max_rows = int(budget / ROW_PRICE)

    leg_results = defaultdict(lambda: {"correct": 0, "total": 0, "picks_sum": 0})

    for gr, dividend, n1_div in rounds_data:
        race_diffs = []
        race_numbers = []
        for race in gr.races:
            diff = predict_difficulty(race)
            ns = len(race.active_entries)
            race_diffs.append((race.race_number, diff, ns))
            race_numbers.append(race.race_number)

        picks = greedy_allocate(race_diffs, max_rows, curves, boundaries)

        for i, race in enumerate(gr.races):
            if not race.result_order:
                continue
            winner_pp = race.result_order[0]
            sorted_entries = sorted(
                race.active_entries,
                key=lambda e: e.super_score, reverse=True
            )
            top_picks = [e.post_position for e in sorted_entries[:picks[i]]]
            diff = race_diffs[i][1]

            # Bucket
            if diff < boundaries[0]:
                b = "easiest"
            elif diff < boundaries[1]:
                b = "easy"
            elif diff < boundaries[2]:
                b = "medium"
            elif diff < boundaries[3]:
                b = "hard"
            else:
                b = "hardest"

            leg_results[b]["total"] += 1
            leg_results[b]["picks_sum"] += picks[i]
            if winner_pp in top_picks:
                leg_results[b]["correct"] += 1

    print(f"\n  {'Difficulty':>10} {'Legs':>5} {'Correct':>7} {'Coverage':>8} {'AvgPicks':>9}")
    print("  " + "─" * 45)
    for b in ["easiest", "easy", "medium", "hard", "hardest"]:
        s = leg_results[b]
        if s["total"] == 0:
            continue
        coverage = s["correct"] / s["total"]
        avg_picks = s["picks_sum"] / s["total"]
        print(f"  {b:>10} {s['total']:>5} {s['correct']:>7} {coverage:>7.1%} {avg_picks:>8.1f}")

    # ═══════════════════════════════════════════════════════════════
    # STATISTICAL ROBUSTNESS: Bootstrap
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  BOOTSTRAP CONFIDENCE (1000 resamples, budget 500kr)")
    print(f"{'═' * 100}")

    budget = 500
    max_rows = int(budget / ROW_PRICE)
    n_bootstrap = 1000

    for strat_label, strat_func in [
        ("greedy_ratio", lambda rd, mr: greedy_allocate(rd, mr, curves, boundaries)),
        ("fixed_2S+rest4", lambda rd, mr: fixed_strategy(rd, 2, 4, mr)),
        ("fixed_3S+rest4", lambda rd, mr: fixed_strategy(rd, 3, 4, mr)),
    ]:
        bootstrap_rois = []
        bootstrap_hits = []

        for _ in range(n_bootstrap):
            sample = random.choices(rounds_data, k=len(rounds_data))
            total_cost = 0
            total_payout = 0
            hits = 0

            for gr, dividend, n1_div in sample:
                race_diffs = []
                race_numbers = []
                for race in gr.races:
                    diff = predict_difficulty(race)
                    ns = len(race.active_entries)
                    race_diffs.append((race.race_number, diff, ns))
                    race_numbers.append(race.race_number)

                picks = strat_func(race_diffs, max_rows)
                correct, total = evaluate_system(gr, picks, race_numbers)
                rows = 1
                for p in picks:
                    rows *= p
                cost = rows * ROW_PRICE

                total_cost += cost
                if correct == total:
                    hits += 1
                    total_payout += dividend

            roi = (total_payout - total_cost) / total_cost if total_cost > 0 else -1
            bootstrap_rois.append(roi)
            bootstrap_hits.append(hits)

        bootstrap_rois.sort()
        bootstrap_hits.sort()
        n = len(bootstrap_rois)

        median_roi = bootstrap_rois[n // 2]
        ci_lo = bootstrap_rois[int(n * 0.025)]
        ci_hi = bootstrap_rois[int(n * 0.975)]
        pct_positive = sum(1 for r in bootstrap_rois if r > 0) / n

        median_hits = bootstrap_hits[n // 2]
        hits_lo = bootstrap_hits[int(n * 0.025)]
        hits_hi = bootstrap_hits[int(n * 0.975)]

        print(f"\n  {strat_label}:")
        print(f"    Hits: median={median_hits}, 95% CI=[{hits_lo}, {hits_hi}]")
        print(f"    ROI:  median={median_roi:+.0%}, 95% CI=[{ci_lo:+.0%}, {ci_hi:+.0%}]")
        print(f"    P(positive ROI) = {pct_positive:.1%}")

    # ═══════════════════════════════════════════════════════════════
    # COMBINED: Best overall approach
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  COMBINED STRATEGY: Round selection + Greedy allocation")
    print("  Budget 300-1500kr, play only top-50% easiest rounds")
    print(f"{'═' * 100}")

    for budget in [300, 500, 750, 1000, 1500]:
        max_rows = int(budget / ROW_PRICE)

        # Compute all rounds with greedy allocation
        round_results = []
        for gr, dividend, n1_div in rounds_data:
            race_diffs = []
            race_numbers = []
            for race in gr.races:
                diff = predict_difficulty(race)
                ns = len(race.active_entries)
                race_diffs.append((race.race_number, diff, ns))
                race_numbers.append(race.race_number)

            picks = greedy_allocate(race_diffs, max_rows, curves, boundaries)
            correct, total = evaluate_system(gr, picks, race_numbers)
            pred_p = round_predicted_hitprob(race_diffs, picks, curves, boundaries)
            rows = 1
            for p in picks:
                rows *= p
            cost = rows * ROW_PRICE

            round_results.append({
                "pred_p": pred_p,
                "hit": correct == total,
                "n1": correct == total - 1,
                "cost": cost,
                "dividend": dividend,
            })

        # Sort by predicted probability
        round_results.sort(key=lambda x: x["pred_p"], reverse=True)

        best_roi = -999
        best_pct = 100
        best_result = None

        for pct in range(10, 101, 5):
            n_play = max(1, int(len(round_results) * pct / 100))
            subset = round_results[:n_play]
            hits = sum(1 for r in subset if r["hit"])
            n1 = sum(1 for r in subset if r["n1"])
            total_cost = sum(r["cost"] for r in subset)
            total_payout = sum(r["dividend"] for r in subset if r["hit"])
            roi = (total_payout - total_cost) / total_cost if total_cost > 0 else -1
            profit = total_payout - total_cost

            if roi > best_roi:
                best_roi = roi
                best_pct = pct
                best_result = {
                    "played": n_play, "hits": hits, "n1": n1,
                    "cost": total_cost, "payout": total_payout,
                    "roi": roi, "profit": profit,
                }

        r = best_result
        print(f"\n  Budget {budget}kr: Bast vid top {best_pct}% "
              f"→ {r['played']} played, {r['hits']} hits, {r['n1']} n-1, "
              f"ROI={r['roi']:+.0%}, Profit={r['profit']:+.0f}kr")

    elapsed = time.time() - t0
    print(f"\n\nTotal runtime: {elapsed:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
