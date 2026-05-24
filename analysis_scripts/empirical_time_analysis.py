#!/usr/bin/env python3
"""Empirical analysis of time data to calibrate time_analysis factor.

Loads ALL cached V75/V85 data, filters future starts, and runs 5 analyses:
1. Volt→Auto offset calibration
2. Distance normalization factor
3. Best trend calculation method
4. Same-distance priority for best_time
5. Predictive power of raw same-distance times
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median, stdev

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from trav_agent.data.atg_client import ATGClient
from trav_agent.data.models import GameRound, StartMethod, Breed

import logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def filter_future_starts(game_round: GameRound) -> None:
    """Remove starts that happened on or after the race date (data leakage)."""
    for race in game_round.races:
        for entry in race.entries:
            entry.horse.past_starts = [
                s for s in entry.horse.past_starts if s.start_date < race.race_date
            ]


async def load_all_rounds() -> list[GameRound]:
    """Load all cached V75+V85 rounds, same approach as fine_tune_weights.py."""
    client = ATGClient()
    all_rounds: list[GameRound] = []
    end = date(2026, 2, 21)

    for gt, start in [("V75", date(2024, 1, 1)), ("V85", date(2024, 3, 1))]:
        logger.info(f"Loading {gt}...")
        async for day, gr in client.fetch_historical_rounds_iter(gt, start, end):
            if gr and gr.is_finished:
                all_rounds.append(gr)

    logger.info(f"Total: {len(all_rounds)} rounds loaded")
    return all_rounds


def analysis_1_volt_auto_offset(all_rounds: list[GameRound]):
    """Analysis 1: Volt→Auto Offset Calibration.

    For horses that have BOTH volt and auto starts at similar distances (within 200m, same breed),
    calculate actual median/mean difference in km_time.
    """
    print("\n" + "=" * 80)
    print("ANALYSIS 1: VOLT → AUTO OFFSET CALIBRATION")
    print("=" * 80)

    # Collect per-horse data: group starts by (horse_name, breed)
    # For each horse, find pairs of volt and auto starts at similar distances

    distance_buckets = {
        "1600-1800m": (1600, 1800),
        "2000-2200m": (2000, 2200),
        "2400-2700m": (2400, 2700),
        "3000+m": (3000, 9999),
    }

    # Track which horses we've already processed to avoid duplicates
    processed_horses = set()

    # For each bucket, collect differences
    bucket_diffs = {name: [] for name in distance_buckets}
    all_diffs = []

    for gr in all_rounds:
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)

        for race in gr_copy.races:
            for entry in race.active_entries:
                horse = entry.horse
                horse_key = (horse.name, race.breed.value)

                if horse_key in processed_horses:
                    continue
                processed_horses.add(horse_key)

                # Get all valid timed starts
                timed = [s for s in horse.past_starts
                         if s.km_time and s.km_time > 0 and s.distance > 0
                         and not s.disqualified and not s.galloped]

                auto_starts = [s for s in timed if s.start_method == StartMethod.AUTO]
                volt_starts = [s for s in timed if s.start_method == StartMethod.VOLT]

                if not auto_starts or not volt_starts:
                    continue

                # For each volt start, find auto starts within 200m
                for vs in volt_starts:
                    matching_autos = [a for a in auto_starts
                                      if abs(a.distance - vs.distance) <= 200]
                    if not matching_autos:
                        continue

                    # Average auto time at similar distance
                    avg_auto = mean([a.km_time for a in matching_autos])
                    diff = vs.km_time - avg_auto  # Positive = volt is slower

                    all_diffs.append(diff)

                    for bname, (lo, hi) in distance_buckets.items():
                        if lo <= vs.distance <= hi:
                            bucket_diffs[bname].append(diff)
                            break

    print(f"\nTotal horse-pairs analyzed: {len(processed_horses)}")
    print(f"Total volt-auto comparisons: {len(all_diffs)}")

    if all_diffs:
        print(f"\nOVERALL (all distances):")
        print(f"  Median diff (volt - auto): {median(all_diffs):.2f}s")
        print(f"  Mean diff (volt - auto):   {mean(all_diffs):.2f}s")
        print(f"  Std dev:                   {stdev(all_diffs):.2f}s")
        print(f"  25th percentile:           {np.percentile(all_diffs, 25):.2f}s")
        print(f"  75th percentile:           {np.percentile(all_diffs, 75):.2f}s")
        print(f"  N:                         {len(all_diffs)}")

    print(f"\nBY DISTANCE BUCKET:")
    for bname in distance_buckets:
        diffs = bucket_diffs[bname]
        if len(diffs) >= 5:
            print(f"  {bname}:")
            print(f"    Median: {median(diffs):.2f}s | Mean: {mean(diffs):.2f}s | "
                  f"Std: {stdev(diffs):.2f}s | N: {len(diffs)}")
        else:
            print(f"  {bname}: N={len(diffs)} (too few)")

    print(f"\n  Current AUTO_VOLT_DIFF = 3.0s")
    if all_diffs:
        print(f"  Empirically recommended: {median(all_diffs):.2f}s (median) or {mean(all_diffs):.2f}s (mean)")


def analysis_2_distance_normalization(all_rounds: list[GameRound]):
    """Analysis 2: Distance Normalization Factor.

    For horses with starts at different distances (same start method, same breed),
    calculate actual km_time difference per 100m distance change.
    """
    print("\n" + "=" * 80)
    print("ANALYSIS 2: DISTANCE NORMALIZATION FACTOR")
    print("=" * 80)

    processed_horses = set()

    # Collect (distance_diff, km_time_diff) pairs, grouped by distance range
    distance_range_pairs = defaultdict(list)  # base_dist_range -> list of (dist_diff_per_100m, time_diff)
    all_pairs = []

    for gr in all_rounds:
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)

        for race in gr_copy.races:
            for entry in race.active_entries:
                horse = entry.horse
                horse_key = (horse.name, race.breed.value)

                if horse_key in processed_horses:
                    continue
                processed_horses.add(horse_key)

                timed = [s for s in horse.past_starts
                         if s.km_time and s.km_time > 0 and s.distance > 0
                         and not s.disqualified and not s.galloped]

                # Group by start method
                for method in [StartMethod.AUTO, StartMethod.VOLT]:
                    method_starts = [s for s in timed if s.start_method == method]
                    if len(method_starts) < 2:
                        continue

                    # Compare all pairs with different distances
                    for i, s1 in enumerate(method_starts):
                        for s2 in method_starts[i+1:]:
                            dist_diff = abs(s2.distance - s1.distance)
                            if dist_diff < 100:  # Need at least 100m difference
                                continue
                            if dist_diff > 1500:  # Too far apart
                                continue

                            # Convention: longer distance should have higher km_time
                            if s2.distance > s1.distance:
                                longer, shorter = s2, s1
                            else:
                                longer, shorter = s1, s2

                            time_diff = longer.km_time - shorter.km_time
                            real_dist_diff = longer.distance - shorter.distance

                            # Per 100m
                            per_100m = time_diff / (real_dist_diff / 100)

                            all_pairs.append((real_dist_diff, per_100m))

                            # Categorize by shorter distance range
                            base_dist = shorter.distance
                            if base_dist <= 1800:
                                distance_range_pairs["short (≤1800m)"].append(per_100m)
                            elif base_dist <= 2200:
                                distance_range_pairs["medium (1801-2200m)"].append(per_100m)
                            elif base_dist <= 2700:
                                distance_range_pairs["long (2201-2700m)"].append(per_100m)
                            else:
                                distance_range_pairs["stayer (2700+m)"].append(per_100m)

    print(f"\nTotal horse data points: {len(processed_horses)}")
    print(f"Total distance-pair comparisons: {len(all_pairs)}")

    if all_pairs:
        all_per_100m = [p[1] for p in all_pairs]
        print(f"\nOVERALL km_time change per 100m distance increase:")
        print(f"  Median: {median(all_per_100m):.3f}s/100m")
        print(f"  Mean:   {mean(all_per_100m):.3f}s/100m")
        print(f"  Std:    {stdev(all_per_100m):.3f}s/100m")
        print(f"  N:      {len(all_per_100m)}")

    print(f"\nBY BASE DISTANCE RANGE:")
    for rng in ["short (≤1800m)", "medium (1801-2200m)", "long (2201-2700m)", "stayer (2700+m)"]:
        vals = distance_range_pairs[rng]
        if len(vals) >= 10:
            print(f"  {rng}:")
            print(f"    Median: {median(vals):.3f}s | Mean: {mean(vals):.3f}s | "
                  f"Std: {stdev(vals):.3f}s | N: {len(vals)}")
        else:
            print(f"  {rng}: N={len(vals)} (too few)")

    # Check linearity: group by actual distance difference
    print(f"\nBY ACTUAL DISTANCE DIFFERENCE (linearity check):")
    diff_buckets = defaultdict(list)
    for dist_diff, per_100m in all_pairs:
        if dist_diff <= 200:
            diff_buckets["100-200m"].append(per_100m)
        elif dist_diff <= 400:
            diff_buckets["201-400m"].append(per_100m)
        elif dist_diff <= 600:
            diff_buckets["401-600m"].append(per_100m)
        elif dist_diff <= 800:
            diff_buckets["601-800m"].append(per_100m)
        else:
            diff_buckets["800+m"].append(per_100m)

    for dname in ["100-200m", "201-400m", "401-600m", "601-800m", "800+m"]:
        vals = diff_buckets[dname]
        if len(vals) >= 10:
            print(f"  Dist diff {dname}: median={median(vals):.3f}s/100m | "
                  f"mean={mean(vals):.3f}s/100m | N={len(vals)}")
        else:
            print(f"  Dist diff {dname}: N={len(vals)} (too few)")

    print(f"\n  Current DISTANCE_FACTOR = 0.8s/100m")
    if all_pairs:
        print(f"  Empirically recommended: {median(all_per_100m):.3f}s/100m (median) or {mean(all_per_100m):.3f}s/100m (mean)")


def analysis_3_trend_methods(all_rounds: list[GameRound]):
    """Analysis 3: Best Trend Calculation Method.

    Test multiple trend calculation approaches and see which best predicts winners.
    """
    print("\n" + "=" * 80)
    print("ANALYSIS 3: BEST TREND CALCULATION METHOD")
    print("=" * 80)

    from trav_agent.analysis.time_analysis import normalize_km_time, REFERENCE_DISTANCE

    # For each race, for each horse, calculate trend scores using different methods
    # Then check correlation with winning

    methods = {
        "A_current": [],  # Current: avg recent 3 norm times vs earlier
        "B_same_dist": [],  # Only same-distance (within 300m) trend
        "C_dist_weighted": [],  # Distance-weighted trend
        "D_raw_time": [],  # Raw time trend, no normalization, same-distance cluster
    }

    wins = []  # 1 if won, 0 otherwise
    top3s = []
    n_races = 0
    n_horses = 0

    for gr in all_rounds:
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)

        for race in gr_copy.races:
            if not race.result_order or not race.active_entries:
                continue

            winner = race.result_order[0]
            top3_set = set(race.result_order[:3])
            n_races += 1

            for entry in race.active_entries:
                horse = entry.horse
                timed = [s for s in horse.past_starts
                         if s.km_time and s.km_time > 0 and s.distance > 0
                         and not s.disqualified and not s.galloped]

                is_winner = 1 if entry.post_position == winner else 0
                is_top3 = 1 if entry.post_position in top3_set else 0
                wins.append(is_winner)
                top3s.append(is_top3)
                n_horses += 1

                # ---- Method A: Current (avg recent 3 normalized vs earlier) ----
                norm_times_a = []
                for s in timed[:10]:
                    dist_diff = abs(s.distance - race.distance) if s.distance > 0 else 0
                    if dist_diff > 800:
                        continue
                    nt = normalize_km_time(
                        s.km_time, s.distance if s.distance > 0 else race.distance,
                        s.start_method, race.breed.value, race.start_method, REFERENCE_DISTANCE
                    )
                    norm_times_a.append(nt)

                if len(norm_times_a) >= 4:
                    recent3 = mean(norm_times_a[:3])
                    earlier = mean(norm_times_a[3:])
                    trend_a = earlier - recent3  # Positive = improving
                else:
                    trend_a = 0.0
                methods["A_current"].append(trend_a)

                # ---- Method B: Only same-distance trend (within 300m of race distance) ----
                same_dist = [s for s in timed[:10]
                             if abs(s.distance - race.distance) <= 300]
                if len(same_dist) >= 4:
                    norm_sd = []
                    for s in same_dist:
                        nt = normalize_km_time(
                            s.km_time, s.distance, s.start_method,
                            race.breed.value, race.start_method, REFERENCE_DISTANCE
                        )
                        norm_sd.append(nt)
                    recent3 = mean(norm_sd[:3])
                    earlier = mean(norm_sd[3:])
                    trend_b = earlier - recent3
                elif len(same_dist) >= 2:
                    norm_sd = []
                    for s in same_dist:
                        nt = normalize_km_time(
                            s.km_time, s.distance, s.start_method,
                            race.breed.value, race.start_method, REFERENCE_DISTANCE
                        )
                        norm_sd.append(nt)
                    trend_b = norm_sd[-1] - norm_sd[0]  # Most recent - oldest
                else:
                    trend_b = 0.0
                methods["B_same_dist"].append(trend_b)

                # ---- Method C: Distance-weighted trend ----
                weighted_times_c = []
                weights_c = []
                for s in timed[:10]:
                    dist_diff = abs(s.distance - race.distance) if s.distance > 0 else 0
                    if dist_diff > 800:
                        continue
                    nt = normalize_km_time(
                        s.km_time, s.distance if s.distance > 0 else race.distance,
                        s.start_method, race.breed.value, race.start_method, REFERENCE_DISTANCE
                    )
                    # Weight: 1.0 at same distance, 0.3 at 600m away
                    w = max(0.3, 1.0 - dist_diff / 600 * 0.7) if dist_diff <= 600 else 0.3
                    weighted_times_c.append(nt)
                    weights_c.append(w)

                if len(weighted_times_c) >= 4:
                    # Weighted recent 3 vs weighted earlier
                    wr3 = sum(t * w for t, w in zip(weighted_times_c[:3], weights_c[:3])) / sum(weights_c[:3])
                    we = sum(t * w for t, w in zip(weighted_times_c[3:], weights_c[3:])) / sum(weights_c[3:])
                    trend_c = we - wr3
                else:
                    trend_c = 0.0
                methods["C_dist_weighted"].append(trend_c)

                # ---- Method D: Raw time trend, same-distance cluster ----
                # Group starts by similar distance (within 200m), use largest cluster
                if same_dist and len(same_dist) >= 2:
                    raw_times = [s.km_time for s in same_dist]
                    if len(raw_times) >= 4:
                        trend_d = mean(raw_times[3:]) - mean(raw_times[:3])
                    elif len(raw_times) >= 2:
                        trend_d = raw_times[-1] - raw_times[0]
                    else:
                        trend_d = 0.0
                else:
                    trend_d = 0.0
                methods["D_raw_time"].append(trend_d)

    print(f"\nRaces analyzed: {n_races}")
    print(f"Horse-entries analyzed: {n_horses}")

    wins_arr = np.array(wins)
    top3_arr = np.array(top3s)

    print(f"\nCORRELATION WITH WINNING (higher = better predictor):")
    print(f"{'Method':<30} {'Corr(win)':>10} {'Corr(top3)':>11} {'Non-zero':>10}")
    for mname, values in methods.items():
        vals = np.array(values)
        # Only compute correlation on non-zero values for clarity
        nonzero = np.count_nonzero(vals)

        if np.std(vals) > 0:
            corr_win = np.corrcoef(vals, wins_arr)[0, 1]
            corr_top3 = np.corrcoef(vals, top3_arr)[0, 1]
        else:
            corr_win = 0.0
            corr_top3 = 0.0

        desc = {
            "A_current": "A: Current (norm, recent3 vs old)",
            "B_same_dist": "B: Same-dist only (within 300m)",
            "C_dist_weighted": "C: Dist-weighted trend",
            "D_raw_time": "D: Raw time trend (same dist)",
        }
        print(f"  {desc[mname]:<30} {corr_win:>9.4f} {corr_top3:>10.4f} {nonzero:>10}")

    # Also test: for each method, what % of the time does the horse with best trend win?
    print(f"\n  PREDICTIVE POWER (trend-ranked):")
    print(f"  For each race, rank horses by trend. How often does best-trend horse win/top3?")

    # Re-iterate races to compute per-race rankings
    method_race_results = {m: {"top1": 0, "top3": 0, "n": 0} for m in methods}

    idx = 0
    for gr in all_rounds:
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)

        for race in gr_copy.races:
            if not race.result_order or not race.active_entries:
                continue

            winner = race.result_order[0]
            top3_set = set(race.result_order[:3])
            n_entries = len(race.active_entries)

            for mname in methods:
                horse_scores = []
                for j, entry in enumerate(race.active_entries):
                    horse_scores.append((entry.post_position, methods[mname][idx + j]))

                # Rank by trend (higher = more improving = better)
                horse_scores.sort(key=lambda x: x[1], reverse=True)

                method_race_results[mname]["n"] += 1
                if horse_scores and horse_scores[0][0] == winner:
                    method_race_results[mname]["top1"] += 1
                if horse_scores and winner in {h[0] for h in horse_scores[:3]}:
                    method_race_results[mname]["top3"] += 1

            idx += n_entries

    for mname, res in method_race_results.items():
        if res["n"] > 0:
            desc = {
                "A_current": "A: Current (norm, recent3 vs old)",
                "B_same_dist": "B: Same-dist only (within 300m)",
                "C_dist_weighted": "C: Dist-weighted trend",
                "D_raw_time": "D: Raw time trend (same dist)",
            }
            print(f"  {desc[mname]:<30} Top-1: {res['top1']/res['n']:.1%} ({res['top1']}/{res['n']})  "
                  f"Top-3: {res['top3']/res['n']:.1%}")


def analysis_4_same_distance_priority(all_rounds: list[GameRound]):
    """Analysis 4: Same-Distance Priority for best_time.

    Compare different best_time calculation strategies.
    """
    print("\n" + "=" * 80)
    print("ANALYSIS 4: SAME-DISTANCE PRIORITY FOR BEST_TIME")
    print("=" * 80)

    from trav_agent.analysis.time_analysis import normalize_km_time, REFERENCE_DISTANCE

    methods = {
        "A_current": [],  # Current: best normalized time from starts ≤600m
        "B_exact_dist": [],  # If ≥2 starts within 100m, use only those
        "C_same_weighted": [],  # best_same + 0.5*best_other
    }

    wins = []
    top3s = []
    n_races = 0

    for gr in all_rounds:
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)

        for race in gr_copy.races:
            if not race.result_order or not race.active_entries:
                continue

            winner = race.result_order[0]
            top3_set = set(race.result_order[:3])
            n_races += 1

            for entry in race.active_entries:
                horse = entry.horse
                timed = [s for s in horse.past_starts
                         if s.km_time and s.km_time > 0 and s.distance > 0
                         and not s.disqualified and not s.galloped]

                is_winner = 1 if entry.post_position == winner else 0
                is_top3 = 1 if entry.post_position in top3_set else 0
                wins.append(is_winner)
                top3s.append(is_top3)

                # Normalize all times
                norm_map = {}  # dist_diff -> list of normalized times
                all_norm = []
                close_norm = []  # ≤600m
                exact_norm = []  # ≤100m
                far_norm = []  # 100-600m

                for s in timed[:10]:
                    dist_diff = abs(s.distance - race.distance)
                    if dist_diff > 800:
                        continue
                    nt = normalize_km_time(
                        s.km_time, s.distance, s.start_method,
                        race.breed.value, race.start_method, REFERENCE_DISTANCE
                    )
                    all_norm.append(nt)
                    if dist_diff <= 600:
                        close_norm.append(nt)
                    if dist_diff <= 100:
                        exact_norm.append(nt)
                    elif dist_diff <= 600:
                        far_norm.append(nt)

                # Method A: Current (best from ≤600m)
                if close_norm:
                    methods["A_current"].append(min(close_norm))
                elif all_norm:
                    methods["A_current"].append(min(all_norm))
                else:
                    methods["A_current"].append(999.0)

                # Method B: If ≥2 exact-distance starts, use only those
                if len(exact_norm) >= 2:
                    methods["B_exact_dist"].append(min(exact_norm))
                elif close_norm:
                    methods["B_exact_dist"].append(min(close_norm))
                elif all_norm:
                    methods["B_exact_dist"].append(min(all_norm))
                else:
                    methods["B_exact_dist"].append(999.0)

                # Method C: Separate pools, weighted
                best_exact = min(exact_norm) if exact_norm else None
                best_far = min(far_norm) if far_norm else None
                if best_exact and best_far:
                    methods["C_same_weighted"].append(best_exact * 0.7 + best_far * 0.3)
                elif best_exact:
                    methods["C_same_weighted"].append(best_exact)
                elif close_norm:
                    methods["C_same_weighted"].append(min(close_norm))
                elif all_norm:
                    methods["C_same_weighted"].append(min(all_norm))
                else:
                    methods["C_same_weighted"].append(999.0)

    print(f"\nRaces: {n_races}")

    wins_arr = np.array(wins)
    top3_arr = np.array(top3s)

    print(f"\nCORRELATION WITH WINNING (lower time = better, so negative correlation = good):")
    for mname, values in methods.items():
        vals = np.array(values)
        # Filter out 999.0 (no data) for correlation
        mask = vals < 900
        if np.sum(mask) > 10 and np.std(vals[mask]) > 0:
            corr_win = np.corrcoef(vals[mask], wins_arr[mask])[0, 1]
            corr_top3 = np.corrcoef(vals[mask], top3_arr[mask])[0, 1]
        else:
            corr_win = 0.0
            corr_top3 = 0.0

        valid = np.sum(mask)
        desc = {
            "A_current": "A: Current (best from ≤600m)",
            "B_exact_dist": "B: Exact dist priority (≤100m if ≥2)",
            "C_same_weighted": "C: Exact*0.7 + Other*0.3",
        }
        print(f"  {desc[mname]:<40} Corr(win): {corr_win:>7.4f}  Corr(top3): {corr_top3:>7.4f}  Valid: {valid}")

    # Per-race ranking analysis
    print(f"\n  PER-RACE RANKING (rank horses by best_time, check winner rank):")

    method_race_results = {m: {"top1": 0, "top3": 0, "n": 0} for m in methods}

    idx = 0
    for gr in all_rounds:
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)

        for race in gr_copy.races:
            if not race.result_order or not race.active_entries:
                continue

            winner = race.result_order[0]
            top3_set = set(race.result_order[:3])
            n_entries = len(race.active_entries)

            for mname in methods:
                horse_scores = []
                for j, entry in enumerate(race.active_entries):
                    horse_scores.append((entry.post_position, methods[mname][idx + j]))

                # Rank by best time (lower = better)
                horse_scores.sort(key=lambda x: x[1])

                method_race_results[mname]["n"] += 1
                if horse_scores and horse_scores[0][0] == winner:
                    method_race_results[mname]["top1"] += 1
                if horse_scores and winner in {h[0] for h in horse_scores[:3]}:
                    method_race_results[mname]["top3"] += 1

            idx += n_entries

    for mname, res in method_race_results.items():
        if res["n"] > 0:
            desc = {
                "A_current": "A: Current (best from ≤600m)",
                "B_exact_dist": "B: Exact dist priority (≤100m if ≥2)",
                "C_same_weighted": "C: Exact*0.7 + Other*0.3",
            }
            print(f"  {desc[mname]:<40} Top-1: {res['top1']/res['n']:.1%} ({res['top1']}/{res['n']})  "
                  f"Top-3: {res['top3']/res['n']:.1%}")


def analysis_5_raw_same_distance(all_rounds: list[GameRound]):
    """Analysis 5: Predictive Power of Raw Same-Distance Times.

    For 2140m auto races: how well does raw best km_time at same distance predict winners?
    """
    print("\n" + "=" * 80)
    print("ANALYSIS 5: PREDICTIVE POWER OF RAW SAME-DISTANCE TIMES")
    print("=" * 80)

    from trav_agent.analysis.time_analysis import normalize_km_time, REFERENCE_DISTANCE

    # Focus on 2140m auto (most common)
    race_results_2140 = {"raw_same": {"top1": 0, "top3": 0, "n": 0},
                          "raw_close": {"top1": 0, "top3": 0, "n": 0},
                          "normalized": {"top1": 0, "top3": 0, "n": 0}}

    # Also general stats
    race_results_all = {"raw_same": {"top1": 0, "top3": 0, "n": 0},
                         "raw_close": {"top1": 0, "top3": 0, "n": 0},
                         "normalized": {"top1": 0, "top3": 0, "n": 0}}

    # Track how many horses have same-distance data
    coverage_stats = {"total_horses": 0, "with_exact_data": 0, "with_close_data": 0}

    # Distance distribution of all races
    dist_counts = defaultdict(int)
    method_dist_counts = defaultdict(int)

    for gr in all_rounds:
        gr_copy = gr.model_copy(deep=True)
        filter_future_starts(gr_copy)

        for race in gr_copy.races:
            if not race.result_order or not race.active_entries:
                continue

            winner = race.result_order[0]
            top3_set = set(race.result_order[:3])
            dist_counts[race.distance] += 1
            method_dist_counts[f"{race.distance}m {race.start_method.value}"] += 1

            is_2140_auto = (abs(race.distance - 2140) <= 100 and
                           race.start_method == StartMethod.AUTO)

            horse_raw_same = []  # (post_pos, best_raw_km_time_at_same_dist)
            horse_raw_close = []
            horse_normalized = []

            for entry in race.active_entries:
                horse = entry.horse
                timed = [s for s in horse.past_starts
                         if s.km_time and s.km_time > 0 and s.distance > 0
                         and not s.disqualified and not s.galloped]

                coverage_stats["total_horses"] += 1

                # Raw same distance (within 100m, same start method)
                exact_same = [s for s in timed
                              if abs(s.distance - race.distance) <= 100
                              and s.start_method == race.start_method]

                if exact_same:
                    coverage_stats["with_exact_data"] += 1
                    best_raw = min(s.km_time for s in exact_same)
                    horse_raw_same.append((entry.post_position, best_raw))
                else:
                    horse_raw_same.append((entry.post_position, 999.0))

                # Raw close distance (within 300m, same start method)
                close_same = [s for s in timed
                              if abs(s.distance - race.distance) <= 300
                              and s.start_method == race.start_method]

                if close_same:
                    coverage_stats["with_close_data"] += 1
                    best_close = min(s.km_time for s in close_same)
                    horse_raw_close.append((entry.post_position, best_close))
                else:
                    horse_raw_close.append((entry.post_position, 999.0))

                # Normalized (current method)
                norm_times = []
                for s in timed[:10]:
                    dist_diff = abs(s.distance - race.distance)
                    if dist_diff > 600:
                        continue
                    nt = normalize_km_time(
                        s.km_time, s.distance, s.start_method,
                        race.breed.value, race.start_method, REFERENCE_DISTANCE
                    )
                    norm_times.append(nt)

                if norm_times:
                    horse_normalized.append((entry.post_position, min(norm_times)))
                else:
                    horse_normalized.append((entry.post_position, 999.0))

            # Evaluate each method
            for method_name, horse_scores in [("raw_same", horse_raw_same),
                                                ("raw_close", horse_raw_close),
                                                ("normalized", horse_normalized)]:
                # Only evaluate if at least 50% of horses have data
                valid = sum(1 for _, t in horse_scores if t < 900)
                if valid < len(horse_scores) * 0.5:
                    continue

                ranked = sorted(horse_scores, key=lambda x: x[1])

                results = race_results_all
                results[method_name]["n"] += 1
                if ranked[0][0] == winner:
                    results[method_name]["top1"] += 1
                if winner in {h[0] for h in ranked[:3]}:
                    results[method_name]["top3"] += 1

                if is_2140_auto:
                    results = race_results_2140
                    results[method_name]["n"] += 1
                    if ranked[0][0] == winner:
                        results[method_name]["top1"] += 1
                    if winner in {h[0] for h in ranked[:3]}:
                        results[method_name]["top3"] += 1

    # Print distance distribution
    print(f"\nDISTANCE DISTRIBUTION (top 15):")
    sorted_dists = sorted(method_dist_counts.items(), key=lambda x: x[1], reverse=True)
    for desc, count in sorted_dists[:15]:
        print(f"  {desc}: {count} races")

    print(f"\nCOVERAGE STATISTICS:")
    print(f"  Total horse-race entries: {coverage_stats['total_horses']}")
    print(f"  With exact-distance data (within 100m, same method): {coverage_stats['with_exact_data']} "
          f"({coverage_stats['with_exact_data']/max(1,coverage_stats['total_horses']):.1%})")
    print(f"  With close-distance data (within 300m, same method): {coverage_stats['with_close_data']} "
          f"({coverage_stats['with_close_data']/max(1,coverage_stats['total_horses']):.1%})")

    print(f"\nALL RACES - PREDICTIVE POWER:")
    for mname, res in race_results_all.items():
        if res["n"] > 0:
            desc = {"raw_same": "Raw same-dist (≤100m, same method)",
                    "raw_close": "Raw close-dist (≤300m, same method)",
                    "normalized": "Current normalized best (≤600m)"}
            print(f"  {desc[mname]:<42} Top-1: {res['top1']/res['n']:.1%} ({res['top1']}/{res['n']})  "
                  f"Top-3: {res['top3']/res['n']:.1%}")

    print(f"\n~2140m AUTO RACES ONLY:")
    for mname, res in race_results_2140.items():
        if res["n"] > 0:
            desc = {"raw_same": "Raw same-dist (≤100m auto)",
                    "raw_close": "Raw close-dist (≤300m auto)",
                    "normalized": "Current normalized best (≤600m)"}
            print(f"  {desc[mname]:<42} Top-1: {res['top1']/res['n']:.1%} ({res['top1']}/{res['n']})  "
                  f"Top-3: {res['top3']/res['n']:.1%}")


async def main():
    start_time = time.time()

    print("=" * 80)
    print("EMPIRICAL TIME ANALYSIS - CALIBRATING time_analysis FACTOR")
    print("=" * 80)
    print(f"Loading all cached V75+V85 data...")

    all_rounds = await load_all_rounds()

    # Count races and horses
    total_races = 0
    total_entries = 0
    for gr in all_rounds:
        for race in gr.races:
            if race.result_order and race.active_entries:
                total_races += 1
                total_entries += len(race.active_entries)

    print(f"\nDataset: {len(all_rounds)} rounds, {total_races} races with results, {total_entries} horse entries")
    print(f"Time to load: {time.time() - start_time:.1f}s")

    # Run all analyses
    analysis_1_volt_auto_offset(all_rounds)
    analysis_2_distance_normalization(all_rounds)
    analysis_3_trend_methods(all_rounds)
    analysis_4_same_distance_priority(all_rounds)
    analysis_5_raw_same_distance(all_rounds)

    print(f"\n{'=' * 80}")
    print(f"SUMMARY OF RECOMMENDATIONS")
    print(f"{'=' * 80}")
    print(f"See each analysis section above for detailed empirical findings.")
    print(f"Total runtime: {time.time() - start_time:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
