#!/usr/bin/env python3
"""Ultimate system optimizer — hitta DEN ENDA bästa strategin.

Dennis: "jag vill inte ha flera olika modeller — en modell och den ska
vara analyserad att det här är det bästa roi och inget annat sätt att
bygga systemet är lönt."

"jag vinner hellre 50 000-250 000kr oftare än att vinna en gång 5 miljoner"

Testar ALLA tänkbara strategier:
1. Antal spikar (0-4), var spikarna sätts (lättast? svårast? mest value?)
2. Garderingsbredd per lopp: fast vs adaptiv
3. Edge-baserad gardering: gardera bara hästar med specifika edges
4. Spika öppna lopp för value
5. Budget-optimering: vad är sweet spot?
6. Round selection: vilka omgångar ska spelas?
7. Utdelnings-targeting: 50k-250k vs allt

Mål: EN strategi med bäst ROI som håller over tid.
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
# Data loading
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
        for entry in race.entries:
            original = entry.horse.past_starts
            filtered = [s for s in original if s.start_date < race.race_date]
            if len(filtered) < len(original):
                entry.horse.past_starts = filtered
            entry.horse.recompute_career_from_starts()
            entry.bet_percentage = None
            entry.odds = None


# ═══════════════════════════════════════════════════════════════════════════
# Strategy implementations
# ═══════════════════════════════════════════════════════════════════════════

def get_race_data(race):
    """Extract analyzed data for a race."""
    sorted_entries = sorted(
        race.active_entries,
        key=lambda e: e.super_score, reverse=True,
    )
    diff = predict_difficulty(race)
    scores = [e.super_score for e in sorted_entries]
    gap_1_2 = scores[0] - scores[1] if len(scores) >= 2 else 0
    gap_2_3 = scores[1] - scores[2] if len(scores) >= 3 else 0
    gap_3_4 = scores[2] - scores[3] if len(scores) >= 4 else 0
    top_score = scores[0] if scores else 0

    return {
        "race": race,
        "sorted_entries": sorted_entries,
        "difficulty": diff,
        "scores": scores,
        "gap_1_2": gap_1_2,
        "gap_2_3": gap_2_3,
        "gap_3_4": gap_3_4,
        "top_score": top_score,
        "n_starters": len(sorted_entries),
        "is_volt": race.start_method == StartMethod.VOLT,
        "has_tillagg": any(
            e.distance > race.distance
            for e in race.active_entries
            if e.distance > 0 and race.distance > 0
        ),
    }


def evaluate_system(gr, picks_per_race):
    """Check if system hits — returns (all_correct, n_correct, n_total)."""
    correct = 0
    total = len(gr.races)
    for i, race in enumerate(gr.races):
        if not race.result_order:
            continue
        winner_pp = race.result_order[0]
        sorted_entries = sorted(
            race.active_entries,
            key=lambda e: e.super_score, reverse=True,
        )
        top_picks = [e.post_position for e in sorted_entries[:picks_per_race[i]]]
        if winner_pp in top_picks:
            correct += 1
    return correct == total, correct, total


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY FAMILY 1: Fixed width strategies
# ═══════════════════════════════════════════════════════════════════════════

def strategy_fixed(race_data_list, n_spikes, rest_width, max_rows):
    """Fixed: N easiest races get 1 pick, rest get rest_width."""
    n = len(race_data_list)
    indexed = sorted(enumerate(race_data_list), key=lambda x: x[1]["difficulty"])

    picks = [0] * n
    for rank, (orig_idx, rd) in enumerate(indexed):
        if rank < n_spikes:
            picks[orig_idx] = 1
        else:
            picks[orig_idx] = min(rest_width, rd["n_starters"])

    # Trim to budget
    _trim_to_budget(picks, max_rows)
    return picks


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY FAMILY 2: Gap-based spike selection
# ═══════════════════════════════════════════════════════════════════════════

def strategy_gap_spike(race_data_list, gap_threshold, rest_width, max_rows):
    """Spike races where gap_1_2 is large (clear favorite). Rest gets width."""
    n = len(race_data_list)
    picks = [0] * n

    for i, rd in enumerate(race_data_list):
        if rd["gap_1_2"] >= gap_threshold:
            picks[i] = 1  # Clear favorite — spike
        else:
            picks[i] = min(rest_width, rd["n_starters"])

    _trim_to_budget(picks, max_rows)
    return picks


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY FAMILY 3: Score-based adaptive width
# ═══════════════════════════════════════════════════════════════════════════

def strategy_score_adaptive(race_data_list, max_rows):
    """Width based on gap between horses.

    - gap 1→2 > 15: spike (1 pick)
    - gap 1→2 > 10: kort (2 picks)
    - gap 1→2 > 5: medel (3 picks)
    - else: bred (4+ picks)
    """
    n = len(race_data_list)
    picks = [0] * n

    for i, rd in enumerate(race_data_list):
        gap = rd["gap_1_2"]
        if gap >= 15:
            picks[i] = 1
        elif gap >= 10:
            picks[i] = 2
        elif gap >= 5:
            picks[i] = 3
        else:
            picks[i] = min(4, rd["n_starters"])

    _trim_to_budget(picks, max_rows)
    return picks


def strategy_score_adaptive_v2(race_data_list, max_rows):
    """V2: also consider gap 2→3 for deciding between 2 and 3 picks."""
    n = len(race_data_list)
    picks = [0] * n

    for i, rd in enumerate(race_data_list):
        gap12 = rd["gap_1_2"]
        gap23 = rd["gap_2_3"]

        if gap12 >= 15:
            picks[i] = 1
        elif gap12 >= 10:
            picks[i] = 2
        elif gap12 >= 5 and gap23 >= 8:
            picks[i] = 2  # Tight top-2 but clear gap after
        elif gap12 >= 5:
            picks[i] = 3
        else:
            picks[i] = min(5, rd["n_starters"])

    _trim_to_budget(picks, max_rows)
    return picks


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY FAMILY 4: Value-spike (spike in open races for surprise value)
# ═══════════════════════════════════════════════════════════════════════════

def strategy_value_spike(race_data_list, max_rows):
    """Dennis's idea: spike an OPEN race for value, go broad on others.

    Spike the hardest race (our #1 pick might surprise at high odds).
    Go broader on easier races to catch them reliably.
    """
    n = len(race_data_list)
    indexed = sorted(enumerate(race_data_list), key=lambda x: x[1]["difficulty"])

    picks = [0] * n
    # Spike the HARDEST race (last in sorted order) — value play
    hardest_idx = indexed[-1][0]
    picks[hardest_idx] = 1

    # Rest gets adaptive width based on difficulty
    for rank, (orig_idx, rd) in enumerate(indexed[:-1]):
        if rd["difficulty"] < 25:
            picks[orig_idx] = min(2, rd["n_starters"])
        elif rd["difficulty"] < 40:
            picks[orig_idx] = min(3, rd["n_starters"])
        else:
            picks[orig_idx] = min(4, rd["n_starters"])

    _trim_to_budget(picks, max_rows)
    return picks


def strategy_value_spike_v2(race_data_list, max_rows):
    """V2: Spike the race with highest top_score (most certain favorite).
    This is different from easiest — it's about model confidence in #1."""
    n = len(race_data_list)
    picks = [0] * n

    # Find race with highest model confidence (top score)
    best_top_score = -1
    best_idx = 0
    for i, rd in enumerate(race_data_list):
        if rd["top_score"] > best_top_score:
            best_top_score = rd["top_score"]
            best_idx = i

    picks[best_idx] = 1

    for i, rd in enumerate(race_data_list):
        if i == best_idx:
            continue
        if rd["gap_1_2"] >= 10:
            picks[i] = min(2, rd["n_starters"])
        elif rd["gap_1_2"] >= 5:
            picks[i] = min(3, rd["n_starters"])
        else:
            picks[i] = min(4, rd["n_starters"])

    _trim_to_budget(picks, max_rows)
    return picks


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY FAMILY 5: Greedy allocators
# ═══════════════════════════════════════════════════════════════════════════

def strategy_greedy_ratio(race_data_list, max_rows, curves, boundaries):
    """Original greedy ratio (v8.0)."""
    race_info = [(rd["race"].race_number, rd["difficulty"], rd["n_starters"])
                 for rd in race_data_list]
    return _greedy_core(race_info, max_rows, curves, boundaries, start_floor=1)


def strategy_greedy_balanced(race_data_list, max_rows, curves, boundaries):
    """Balanced greedy: floor of 2 picks, then greedy (v8.2)."""
    race_info = [(rd["race"].race_number, rd["difficulty"], rd["n_starters"])
                 for rd in race_data_list]
    return _greedy_core(race_info, max_rows, curves, boundaries, start_floor=2)


def strategy_greedy_floor3(race_data_list, max_rows, curves, boundaries):
    """Aggressive greedy: floor of 3 picks, then greedy."""
    race_info = [(rd["race"].race_number, rd["difficulty"], rd["n_starters"])
                 for rd in race_data_list]
    return _greedy_core(race_info, max_rows, curves, boundaries, start_floor=3)


def _greedy_core(race_info, max_rows, curves, boundaries, start_floor=1):
    """Core greedy allocator with configurable floor."""
    n = len(race_info)
    picks = [1] * n

    def total_rows():
        r = 1
        for p in picks:
            r *= p
        return r

    # Phase 1: Floor
    if start_floor > 1:
        floor_order = sorted(
            range(n),
            key=lambda i: _get_cov_prob(race_info[i][1], 1, curves, boundaries),
        )
        for i in floor_order:
            rn, diff, ns = race_info[i]
            while picks[i] < min(start_floor, ns):
                new_rows = total_rows() * (picks[i] + 1) // picks[i]
                if new_rows > max_rows:
                    break
                picks[i] += 1

    # Phase 2: Greedy expansion
    for _ in range(150):
        if total_rows() >= max_rows:
            break

        best_ratio = -float("inf")
        best_idx = -1

        for i, (rn, diff, ns) in enumerate(race_info):
            if picks[i] >= ns:
                continue
            new_rows = total_rows() * (picks[i] + 1) // picks[i]
            if new_rows > max_rows:
                continue

            old_p = _get_cov_prob(diff, picks[i], curves, boundaries)
            new_p = _get_cov_prob(diff, picks[i] + 1, curves, boundaries)
            gain = math.log(max(new_p, 1e-10)) - math.log(max(old_p, 1e-10))
            cost = math.log(picks[i] + 1) - math.log(picks[i])
            ratio = gain / max(cost, 1e-10)

            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i

        if best_idx < 0:
            break
        picks[best_idx] += 1

    return picks


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY FAMILY 6: Hybrid — spike 1 + greedy rest
# ═══════════════════════════════════════════════════════════════════════════

def strategy_hybrid_1spike_greedy(race_data_list, max_rows, curves, boundaries):
    """Spike the easiest race, then greedy-allocate the rest."""
    n = len(race_data_list)
    indexed = sorted(enumerate(race_data_list), key=lambda x: x[1]["difficulty"])
    spike_idx = indexed[0][0]  # Easiest race

    picks = [1] * n
    picks[spike_idx] = 1  # Already 1

    # For the non-spike races, greedy allocate with remaining budget
    # But first give all non-spike races a floor of 2
    def total_rows():
        r = 1
        for p in picks:
            r *= p
        return r

    non_spike = [i for i in range(n) if i != spike_idx]
    for i in sorted(non_spike, key=lambda i: race_data_list[i]["difficulty"]):
        ns = race_data_list[i]["n_starters"]
        if picks[i] < min(2, ns) and total_rows() * 2 <= max_rows:
            picks[i] = 2

    # Greedy expansion on non-spike races
    for _ in range(100):
        if total_rows() >= max_rows:
            break
        best_ratio = -float("inf")
        best_idx = -1
        for i in non_spike:
            ns = race_data_list[i]["n_starters"]
            if picks[i] >= ns:
                continue
            new_rows = total_rows() * (picks[i] + 1) // picks[i]
            if new_rows > max_rows:
                continue
            diff = race_data_list[i]["difficulty"]
            old_p = _get_cov_prob(diff, picks[i], curves, boundaries)
            new_p = _get_cov_prob(diff, picks[i] + 1, curves, boundaries)
            gain = math.log(max(new_p, 1e-10)) - math.log(max(old_p, 1e-10))
            cost = math.log(picks[i] + 1) - math.log(picks[i])
            ratio = gain / max(cost, 1e-10)
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i
        if best_idx < 0:
            break
        picks[best_idx] += 1

    return picks


def strategy_hybrid_gap_greedy(race_data_list, max_rows, curves, boundaries):
    """Spike races with big gap (>12), then greedy rest."""
    n = len(race_data_list)
    picks = [1] * n
    non_spike = []

    for i, rd in enumerate(race_data_list):
        if rd["gap_1_2"] >= 12:
            picks[i] = 1  # Spike
        else:
            non_spike.append(i)

    # Floor of 2 for non-spike
    def total_rows():
        r = 1
        for p in picks:
            r *= p
        return r

    for i in non_spike:
        ns = race_data_list[i]["n_starters"]
        if picks[i] < min(2, ns) and total_rows() * 2 <= max_rows:
            picks[i] = 2

    # Greedy on non-spike
    for _ in range(100):
        if total_rows() >= max_rows:
            break
        best_ratio = -float("inf")
        best_idx = -1
        for i in non_spike:
            ns = race_data_list[i]["n_starters"]
            if picks[i] >= ns:
                continue
            new_rows = total_rows() * (picks[i] + 1) // picks[i]
            if new_rows > max_rows:
                continue
            diff = race_data_list[i]["difficulty"]
            old_p = _get_cov_prob(diff, picks[i], curves, boundaries)
            new_p = _get_cov_prob(diff, picks[i] + 1, curves, boundaries)
            gain = math.log(max(new_p, 1e-10)) - math.log(max(old_p, 1e-10))
            cost = math.log(picks[i] + 1) - math.log(picks[i])
            ratio = gain / max(cost, 1e-10)
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i
        if best_idx < 0:
            break
        picks[best_idx] += 1

    return picks


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY FAMILY 7: Edge-mix gardering
# ═══════════════════════════════════════════════════════════════════════════

def strategy_edge_gardering(race_data_list, max_rows):
    """Gardera bara hästar som har specifika edges.

    In each race, pick horses that have at least one strong edge:
    - High time_analysis score (>65)
    - High prize_index (>70)
    - Good post_position (>65) in relevant start method
    - Spike if only 1 horse has edge, broader if many do
    """
    n = len(race_data_list)
    picks = [0] * n

    for i, rd in enumerate(race_data_list):
        edge_count = 0
        for entry in rd["sorted_entries"]:
            has_edge = False
            fs = entry.factor_scores
            if fs.get("time_analysis", 0) > 65:
                has_edge = True
            if fs.get("prize_index", 0) > 70:
                has_edge = True
            if fs.get("post_position", 0) > 65:
                has_edge = True
            if fs.get("form_curve", 0) > 70:
                has_edge = True
            if has_edge:
                edge_count += 1

        # Limit picks to horses with edges, but at least 1
        picks[i] = max(1, min(edge_count, rd["n_starters"]))

    _trim_to_budget(picks, max_rows)
    return picks


def strategy_top_n_by_score(race_data_list, score_threshold, max_rows):
    """Pick all horses above a score threshold in each race."""
    n = len(race_data_list)
    picks = [0] * n

    for i, rd in enumerate(race_data_list):
        count = 0
        for entry in rd["sorted_entries"]:
            if entry.super_score >= score_threshold:
                count += 1
        picks[i] = max(1, min(count, rd["n_starters"]))

    _trim_to_budget(picks, max_rows)
    return picks


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _trim_to_budget(picks, max_rows):
    """Reduce picks to fit within budget, trimming widest first."""
    rows = 1
    for p in picks:
        rows *= p
    while rows > max_rows:
        widest_idx = -1
        widest_val = 0
        for i in range(len(picks)):
            if picks[i] > 1 and picks[i] > widest_val:
                widest_val = picks[i]
                widest_idx = i
        if widest_idx < 0:
            break
        picks[widest_idx] -= 1
        rows = 1
        for p in picks:
            rows *= p


def build_coverage_curves(rounds_data):
    """Build empirical coverage curves."""
    observations = []
    for gr, _, _ in rounds_data:
        for race in gr.races:
            if not race.result_order:
                continue
            winner_pp = race.result_order[0]
            sorted_entries = sorted(
                race.active_entries,
                key=lambda e: e.super_score, reverse=True,
            )
            rank = next(
                (i + 1 for i, e in enumerate(sorted_entries)
                 if e.post_position == winner_pp),
                len(sorted_entries),
            )
            diff = predict_difficulty(race)
            observations.append({"difficulty": diff, "winner_rank": rank})

    if len(observations) < 50:
        return None

    diffs = sorted(o["difficulty"] for o in observations)
    n = len(diffs)
    boundaries = [diffs[n // 5], diffs[2 * n // 5],
                  diffs[3 * n // 5], diffs[4 * n // 5]]

    def get_bucket(d):
        for bi, b in enumerate(boundaries):
            if d < b:
                return bi
        return 4

    curves = {}
    for b in range(5):
        bucket_obs = [o for o in observations if get_bucket(o["difficulty"]) == b]
        total = len(bucket_obs)
        if total == 0:
            continue
        probs = {}
        for k in range(1, 16):
            probs[k] = sum(1 for o in bucket_obs if o["winner_rank"] <= k) / total
        curves[b] = {"probs": probs, "count": total}

    return curves, boundaries


def _get_cov_prob(difficulty, picks, curves, boundaries):
    """Get coverage probability from curves."""
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
    picks = min(max(picks, 1), 15)
    return curves[b]["probs"].get(picks, 0.95)


# ═══════════════════════════════════════════════════════════════════════════
# Main analysis
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    t0 = time.time()
    client = ATGClient()
    analyzer = CompositeAnalyzer(DEFAULT_CONFIG)

    print("=" * 100)
    print("  ULTIMATE SYSTEM OPTIMIZER")
    print("  Dennis: 'en modell — den bästa, inget annat sätt är lönt'")
    print("=" * 100)

    # Load all available rounds
    round_infos = find_round_ids()
    random.shuffle(round_infos)
    round_infos = round_infos[:350]  # More data for robustness

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

    print(f"\nLoaded {loaded} rounds, {sum(len(g.races) for g, _, _ in rounds_data)} races")

    # Build coverage curves
    result = build_coverage_curves(rounds_data)
    if not result:
        print("Not enough data!")
        return
    curves, boundaries = result

    # ═══════════════════════════════════════════════════════════════
    # TEST ALL STRATEGIES
    # ═══════════════════════════════════════════════════════════════

    budgets = [300, 500, 750, 1000]

    # Results collector
    all_results = {}

    for budget in budgets:
        max_rows = int(budget / ROW_PRICE)

        print(f"\n{'═' * 100}")
        print(f"  BUDGET: {budget} kr ({max_rows} rader)")
        print(f"{'═' * 100}")

        strategies = {}

        def register(name):
            strategies[name] = {
                "hits": 0, "n1": 0, "n2": 0, "tested": 0,
                "total_cost": 0, "total_payout": 0,
                "dividends_hit": [],
                "per_round": [],  # For bootstrap
            }

        # Register all strategies
        # Family 1: Fixed
        for ns in [0, 1, 2, 3]:
            for rw in [3, 4, 5, 6]:
                register(f"fixed_{ns}S+rest{rw}")

        # Family 2: Gap-based spike
        for gt in [8, 10, 12, 15]:
            for rw in [3, 4, 5]:
                register(f"gap{gt}_rest{rw}")

        # Family 3: Score-adaptive
        register("score_adaptive_v1")
        register("score_adaptive_v2")

        # Family 4: Value spike
        register("value_spike")
        register("value_spike_v2")

        # Family 5: Greedy
        register("greedy_ratio")
        register("greedy_balanced")
        register("greedy_floor3")

        # Family 6: Hybrid
        register("hybrid_1spike_greedy")
        register("hybrid_gap_greedy")

        # Family 7: Edge-based
        register("edge_gardering")
        for threshold in [45, 50, 55, 60]:
            register(f"top_score_{threshold}")

        # Run all strategies on all rounds
        for gr, dividend, n1_div in rounds_data:
            race_data_list = [get_race_data(race) for race in gr.races]

            # Compute picks for each strategy
            strategy_picks = {}

            # Family 1: Fixed
            for ns in [0, 1, 2, 3]:
                for rw in [3, 4, 5, 6]:
                    name = f"fixed_{ns}S+rest{rw}"
                    strategy_picks[name] = strategy_fixed(
                        race_data_list, ns, rw, max_rows)

            # Family 2: Gap-based
            for gt in [8, 10, 12, 15]:
                for rw in [3, 4, 5]:
                    name = f"gap{gt}_rest{rw}"
                    strategy_picks[name] = strategy_gap_spike(
                        race_data_list, gt, rw, max_rows)

            # Family 3: Score-adaptive
            strategy_picks["score_adaptive_v1"] = strategy_score_adaptive(
                race_data_list, max_rows)
            strategy_picks["score_adaptive_v2"] = strategy_score_adaptive_v2(
                race_data_list, max_rows)

            # Family 4: Value spike
            strategy_picks["value_spike"] = strategy_value_spike(
                race_data_list, max_rows)
            strategy_picks["value_spike_v2"] = strategy_value_spike_v2(
                race_data_list, max_rows)

            # Family 5: Greedy
            strategy_picks["greedy_ratio"] = strategy_greedy_ratio(
                race_data_list, max_rows, curves, boundaries)
            strategy_picks["greedy_balanced"] = strategy_greedy_balanced(
                race_data_list, max_rows, curves, boundaries)
            strategy_picks["greedy_floor3"] = strategy_greedy_floor3(
                race_data_list, max_rows, curves, boundaries)

            # Family 6: Hybrid
            strategy_picks["hybrid_1spike_greedy"] = strategy_hybrid_1spike_greedy(
                race_data_list, max_rows, curves, boundaries)
            strategy_picks["hybrid_gap_greedy"] = strategy_hybrid_gap_greedy(
                race_data_list, max_rows, curves, boundaries)

            # Family 7: Edge-based
            strategy_picks["edge_gardering"] = strategy_edge_gardering(
                race_data_list, max_rows)
            for threshold in [45, 50, 55, 60]:
                name = f"top_score_{threshold}"
                strategy_picks[name] = strategy_top_n_by_score(
                    race_data_list, threshold, max_rows)

            # Evaluate all
            for name, picks in strategy_picks.items():
                hit, correct, total = evaluate_system(gr, picks)
                rows = 1
                for p in picks:
                    rows *= p
                cost = rows * ROW_PRICE

                s = strategies[name]
                s["tested"] += 1
                s["total_cost"] += cost

                payout = 0
                if hit:
                    s["hits"] += 1
                    s["total_payout"] += dividend
                    s["dividends_hit"].append(dividend)
                    payout = dividend
                elif correct == total - 1:
                    s["n1"] += 1
                elif correct == total - 2:
                    s["n2"] += 1

                s["per_round"].append({
                    "cost": cost, "payout": payout, "hit": hit,
                    "n1": correct == total - 1,
                })

        # ── Print results sorted by ROI ────────────────────────
        print(f"\n  {'Strategy':<25} {'Hits':>4} {'N-1':>4} {'HitRate':>7} "
              f"{'AvgCost':>8} {'ROI':>8} {'AvgDiv':>8} {'Profit':>10}")
        print("  " + "─" * 85)

        sorted_strats = sorted(
            strategies.items(),
            key=lambda x: (
                (x[1]["total_payout"] - x[1]["total_cost"]) / x[1]["total_cost"]
                if x[1]["total_cost"] > 0 else -999
            ),
            reverse=True,
        )

        for name, s in sorted_strats[:30]:
            if s["tested"] == 0 or s["hits"] == 0:
                continue
            hitrate = s["hits"] / s["tested"]
            avg_cost = s["total_cost"] / s["tested"]
            roi = (s["total_payout"] - s["total_cost"]) / s["total_cost"]
            avg_div = statistics.mean(s["dividends_hit"]) if s["dividends_hit"] else 0
            profit = s["total_payout"] - s["total_cost"]

            # Mark families
            if name.startswith("greedy") or name.startswith("hybrid"):
                marker = " ◀"
            elif name.startswith("gap") or name.startswith("score") or name.startswith("value") or name.startswith("edge") or name.startswith("top_score"):
                marker = " ★"
            else:
                marker = ""

            print(f"  {name:<25} {s['hits']:>4} {s['n1']:>4} {hitrate:>6.1%} "
                  f"{avg_cost:>7.0f}kr {roi:>+7.0%} {avg_div:>7.0f}kr {profit:>+9.0f}kr{marker}")

        all_results[budget] = strategies

    # ═══════════════════════════════════════════════════════════════
    # DIVIDEND ANALYSIS: Which strategies hit big rounds?
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  DIVIDEND ANALYSIS: Vilka strategier vinner 50k+ omgångar?")
    print(f"{'═' * 100}")

    budget = 500
    strategies = all_results[budget]

    print(f"\n  Budget {budget}kr — Vinster per utdelningsklass:")
    print(f"  {'Strategy':<25} {'<10k':>5} {'10-50k':>6} {'50-250k':>7} {'250k+':>5} {'Total':>5}")
    print("  " + "─" * 55)

    sorted_by_hits = sorted(strategies.items(), key=lambda x: x[1]["hits"], reverse=True)
    for name, s in sorted_by_hits[:20]:
        if s["hits"] == 0:
            continue
        divs = s["dividends_hit"]
        d_10k = sum(1 for d in divs if d < 10000)
        d_50k = sum(1 for d in divs if 10000 <= d < 50000)
        d_250k = sum(1 for d in divs if 50000 <= d < 250000)
        d_big = sum(1 for d in divs if d >= 250000)
        print(f"  {name:<25} {d_10k:>5} {d_50k:>6} {d_250k:>7} {d_big:>5} {len(divs):>5}")

    # ═══════════════════════════════════════════════════════════════
    # BOOTSTRAP: Top 5 strategies
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  BOOTSTRAP: Topp 5 strategier (1000 resamples, budget 500kr)")
    print(f"{'═' * 100}")

    budget = 500
    strategies = all_results[budget]
    n_bootstrap = 1000

    # Pick top 5 by ROI (with at least 3 hits)
    top_strats = sorted(
        [(name, s) for name, s in strategies.items() if s["hits"] >= 3],
        key=lambda x: (x[1]["total_payout"] - x[1]["total_cost"]) / max(x[1]["total_cost"], 1),
        reverse=True,
    )[:8]

    for name, s in top_strats:
        round_data = s["per_round"]
        bootstrap_rois = []

        for _ in range(n_bootstrap):
            sample = random.choices(round_data, k=len(round_data))
            total_cost = sum(r["cost"] for r in sample)
            total_payout = sum(r["payout"] for r in sample)
            roi = (total_payout - total_cost) / total_cost if total_cost > 0 else -1
            bootstrap_rois.append(roi)

        bootstrap_rois.sort()
        n = len(bootstrap_rois)
        median_roi = bootstrap_rois[n // 2]
        ci_lo = bootstrap_rois[int(n * 0.025)]
        ci_hi = bootstrap_rois[int(n * 0.975)]
        pct_positive = sum(1 for r in bootstrap_rois if r > 0) / n

        print(f"\n  {name}:")
        print(f"    Hits: {s['hits']}/{s['tested']}")
        print(f"    ROI: median={median_roi:+.0%}, 95% CI=[{ci_lo:+.0%}, {ci_hi:+.0%}]")
        print(f"    P(positiv ROI) = {pct_positive:.1%}")

    # ═══════════════════════════════════════════════════════════════
    # ROUND SELECTION: Top strategies + round filtering
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  ROUND SELECTION: Topp 3 strategier × omgångsfiltrering")
    print(f"{'═' * 100}")

    for budget in [300, 500, 750]:
        max_rows = int(budget / ROW_PRICE)
        strategies = all_results[budget]

        # Top 3 by ROI
        top3 = sorted(
            [(name, s) for name, s in strategies.items() if s["hits"] >= 3],
            key=lambda x: (x[1]["total_payout"] - x[1]["total_cost"]) / max(x[1]["total_cost"], 1),
            reverse=True,
        )[:3]

        print(f"\n  Budget {budget}kr:")
        for name, s in top3:
            round_data = s["per_round"]
            # We need predicted probabilities — use round difficulty as proxy
            # Sort by cost (lower cost = easier round)
            indexed = list(enumerate(round_data))

            # Test filtering: play only rounds where cost is below median
            all_roi = (s["total_payout"] - s["total_cost"]) / s["total_cost"] if s["total_cost"] > 0 else 0

            # Try different percentile cuts
            best_roi_filtered = all_roi
            best_pct = 100

            for pct in [100, 85, 75, 65, 50]:
                n_play = max(1, int(len(round_data) * pct / 100))
                # Sort by cost (cheapest rounds = easiest)
                sorted_rounds = sorted(round_data, key=lambda r: r["cost"])
                subset = sorted_rounds[:n_play]
                tc = sum(r["cost"] for r in subset)
                tp = sum(r["payout"] for r in subset)
                roi_f = (tp - tc) / tc if tc > 0 else -1
                if roi_f > best_roi_filtered:
                    best_roi_filtered = roi_f
                    best_pct = pct

            hits = s["hits"]
            print(f"    {name:<25} {hits:>3} hits, ROI={all_roi:+.0%} "
                  f"→ best with top {best_pct}% = {best_roi_filtered:+.0%}")

    # ═══════════════════════════════════════════════════════════════
    # FINAL VERDICT
    # ═══════════════════════════════════════════════════════════════

    print(f"\n\n{'═' * 100}")
    print("  FINAL VERDICT: Bästa strategin per budget")
    print(f"{'═' * 100}")

    for budget in [300, 500, 750, 1000]:
        strategies = all_results[budget]
        best = max(
            [(name, s) for name, s in strategies.items() if s["hits"] >= 2],
            key=lambda x: (x[1]["total_payout"] - x[1]["total_cost"]) / max(x[1]["total_cost"], 1),
        )
        name, s = best
        roi = (s["total_payout"] - s["total_cost"]) / s["total_cost"]
        profit = s["total_payout"] - s["total_cost"]
        hitrate = s["hits"] / s["tested"]
        print(f"  {budget:>5}kr: {name:<25} {s['hits']:>3} hits ({hitrate:.1%}), "
              f"ROI={roi:+.0%}, Profit={profit:+.0f}kr")

    elapsed = time.time() - t0
    print(f"\n\nTotal runtime: {elapsed:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
