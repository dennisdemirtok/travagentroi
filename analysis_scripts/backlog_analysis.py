#!/usr/bin/env python3
"""
Backlog diagnostic analysis: Why does the model perform well historically but fail live?
Analyzes 13,485 entries from backlog.json across time periods.
"""

import json
import sys
from collections import defaultdict, Counter
from datetime import datetime
import statistics

print("Loading backlog.json...")
with open("backlog.json", "r") as f:
    data = json.load(f)
entries = data["entries"]
print(f"Loaded {len(entries)} entries\n")

# ============================================================
# PERIOD DEFINITIONS
# ============================================================
def get_period(date_str):
    if date_str < "2025-01-01":
        return "PRE-2025 (historical)"
    elif date_str < "2026-01-01":
        return "2025 (historical)"
    elif date_str < "2026-03-01":
        return "Jan-Feb 2026 (transition)"
    else:
        return "Mar-Apr 2026 (live)"

def get_year_month(date_str):
    return date_str[:7]

# ============================================================
# 1. PERFORMANCE BY TIME PERIOD
# ============================================================
print("=" * 80)
print("1. PERFORMANCE BY TIME PERIOD")
print("=" * 80)

period_stats = defaultdict(lambda: {
    "count": 0, "hits": 0, "total_cost": 0.0, "total_payout": 0.0,
    "confidences": [], "race_correct": 0, "race_total": 0,
    "strategies": defaultdict(lambda: {"count": 0, "hits": 0, "cost": 0.0, "payout": 0.0})
})

for e in entries:
    p = get_period(e["date"])
    ps = period_stats[p]
    ps["count"] += 1
    ps["hits"] += 1 if e["hit"] else 0
    ps["total_cost"] += e["cost"]
    ps["total_payout"] += e["payout"]
    ps["confidences"].append(e["avg_confidence"])

    strat = e["strategy"]
    ps["strategies"][strat]["count"] += 1
    ps["strategies"][strat]["hits"] += 1 if e["hit"] else 0
    ps["strategies"][strat]["cost"] += e["cost"]
    ps["strategies"][strat]["payout"] += e["payout"]

    for r in e.get("races", []):
        ps["race_total"] += 1
        ps["race_correct"] += 1 if r.get("ratt") else 0

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    ps = period_stats[period]
    if ps["count"] == 0:
        continue
    hit_rate = ps["hits"] / ps["count"] * 100
    roi = (ps["total_payout"] - ps["total_cost"]) / ps["total_cost"] * 100 if ps["total_cost"] > 0 else 0
    avg_conf = statistics.mean(ps["confidences"]) if ps["confidences"] else 0
    race_acc = ps["race_correct"] / ps["race_total"] * 100 if ps["race_total"] > 0 else 0

    print(f"\n--- {period} ---")
    print(f"  Entries: {ps['count']}, Hits: {ps['hits']}, Hit rate: {hit_rate:.1f}%")
    print(f"  Total cost: {ps['total_cost']:.0f}, Total payout: {ps['total_payout']:.0f}")
    print(f"  ROI: {roi:.1f}%")
    print(f"  Avg confidence: {avg_conf:.1f}")
    print(f"  Per-race accuracy: {ps['race_correct']}/{ps['race_total']} = {race_acc:.1f}%")

# ============================================================
# 2. ROI BY STRATEGY PER PERIOD
# ============================================================
print("\n\n" + "=" * 80)
print("2. ROI BY STRATEGY PER PERIOD")
print("=" * 80)

all_strategies = sorted(set(e["strategy"] for e in entries))
for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    ps = period_stats[period]
    print(f"\n--- {period} ---")
    print(f"  {'Strategy':<22} {'N':>5} {'Hits':>5} {'Hit%':>6} {'Cost':>10} {'Payout':>10} {'ROI%':>8}")
    for strat in all_strategies:
        ss = ps["strategies"][strat]
        if ss["count"] == 0:
            continue
        hr = ss["hits"] / ss["count"] * 100
        roi = (ss["payout"] - ss["cost"]) / ss["cost"] * 100 if ss["cost"] > 0 else 0
        print(f"  {strat:<22} {ss['count']:>5} {ss['hits']:>5} {hr:>5.1f}% {ss['cost']:>10.0f} {ss['payout']:>10.0f} {roi:>7.1f}%")

# ============================================================
# 3. CONFIDENCE vs ACTUAL CORRECTNESS (DATA LEAKAGE CHECK)
# ============================================================
print("\n\n" + "=" * 80)
print("3. CONFIDENCE vs ACTUAL CORRECTNESS — DATA LEAKAGE ANALYSIS")
print("=" * 80)

# Confidence distribution by period
for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    confs = period_stats[period]["confidences"]
    if not confs:
        continue
    print(f"\n--- {period} ---")
    print(f"  Avg confidence: {statistics.mean(confs):.1f}")
    print(f"  Median confidence: {statistics.median(confs):.1f}")
    print(f"  Stdev: {statistics.stdev(confs):.1f}" if len(confs) > 1 else "")
    # Confidence buckets
    buckets = defaultdict(lambda: {"correct": 0, "total": 0})
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    for e in period_entries:
        for r in e.get("races", []):
            conf = r.get("conf", 0)
            bucket = int(conf // 10) * 10
            buckets[bucket]["total"] += 1
            buckets[bucket]["correct"] += 1 if r.get("ratt") else 0
    print(f"  {'Conf bucket':<14} {'Correct':>8} {'Total':>8} {'Accuracy':>8}")
    for b in sorted(buckets.keys()):
        acc = buckets[b]["correct"] / buckets[b]["total"] * 100 if buckets[b]["total"] > 0 else 0
        print(f"  {b:>3}-{b+9:<3}        {buckets[b]['correct']:>8} {buckets[b]['total']:>8} {acc:>7.1f}%")

# ============================================================
# 4. CONFIDENCE CALIBRATION — KEY LEAKAGE INDICATOR
# ============================================================
print("\n\n" + "=" * 80)
print("4. CONFIDENCE CALIBRATION (confidence vs race accuracy)")
print("=" * 80)

# If the model is well-calibrated, 80% confidence should yield ~80% accuracy
# If historical accuracy >> confidence, it's not leaking (model is conservative)
# If historical accuracy << confidence but live accuracy << confidence, the model may be leaking

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    # Per-race: compare confidence to actual correctness
    correct_confs = []
    incorrect_confs = []
    for e in period_entries:
        for r in e.get("races", []):
            conf = r.get("conf", 0)
            if r.get("ratt"):
                correct_confs.append(conf)
            else:
                incorrect_confs.append(conf)

    avg_correct = statistics.mean(correct_confs) if correct_confs else 0
    avg_incorrect = statistics.mean(incorrect_confs) if incorrect_confs else 0

    print(f"\n--- {period} ---")
    print(f"  Avg confidence when CORRECT:   {avg_correct:.1f}  (n={len(correct_confs)})")
    print(f"  Avg confidence when INCORRECT: {avg_incorrect:.1f}  (n={len(incorrect_confs)})")
    print(f"  Confidence spread (correct - incorrect): {avg_correct - avg_incorrect:.1f}")

# ============================================================
# 5. TOP PICK (A-RANK / HIGHEST SCORE) WIN RATE
# ============================================================
print("\n\n" + "=" * 80)
print("5. TOP PICK WIN RATE (highest-score horse per race)")
print("=" * 80)

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    top_wins = 0
    top_total = 0
    top_scores_when_win = []
    top_scores_when_lose = []

    for e in period_entries:
        for r in e.get("races", []):
            pl = r.get("pick_list", [])
            if not pl:
                continue
            # Find highest-score pick
            top_pick = max(pl, key=lambda x: x.get("score", 0))
            top_total += 1
            winner_nr = r.get("vinnare")
            if top_pick["nr"] == winner_nr:
                top_wins += 1
                top_scores_when_win.append(top_pick["score"])
            else:
                top_scores_when_lose.append(top_pick["score"])

    win_rate = top_wins / top_total * 100 if top_total > 0 else 0
    avg_score_win = statistics.mean(top_scores_when_win) if top_scores_when_win else 0
    avg_score_lose = statistics.mean(top_scores_when_lose) if top_scores_when_lose else 0

    print(f"\n--- {period} ---")
    print(f"  Top pick wins: {top_wins}/{top_total} = {win_rate:.1f}%")
    print(f"  Avg top-pick score when WINS:  {avg_score_win:.1f}")
    print(f"  Avg top-pick score when LOSES: {avg_score_lose:.1f}")

# ============================================================
# 6. PICKS PER RACE — SYSTEM BUILDING ANALYSIS
# ============================================================
print("\n\n" + "=" * 80)
print("6. PICKS PER RACE — SYSTEM BUILDING ANALYSIS")
print("=" * 80)

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    picks_dist = Counter()
    picks_acc = defaultdict(lambda: {"correct": 0, "total": 0})
    starters_when_wrong = []

    for e in period_entries:
        for r in e.get("races", []):
            n_picks = r.get("picks", len(r.get("pick_list", [])))
            picks_dist[n_picks] += 1
            picks_acc[n_picks]["total"] += 1
            if r.get("ratt"):
                picks_acc[n_picks]["correct"] += 1
            else:
                starters_when_wrong.append(r.get("startande", 0))

    print(f"\n--- {period} ---")
    print(f"  {'Picks':>5} {'Count':>8} {'Correct':>8} {'Accuracy':>8} {'Coverage':>10}")
    for n in sorted(picks_dist.keys()):
        acc = picks_acc[n]["correct"] / picks_acc[n]["total"] * 100 if picks_acc[n]["total"] > 0 else 0
        # Average coverage (picks/starters) is estimated
        print(f"  {n:>5} {picks_dist[n]:>8} {picks_acc[n]['correct']:>8} {acc:>7.1f}%")

# ============================================================
# 7. OPTIMAL PICKS PER RACE ANALYSIS
# ============================================================
print("\n\n" + "=" * 80)
print("7. OPTIMAL PICKS ANALYSIS — WHERE DOES THE WINNER RANK?")
print("=" * 80)

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    # For each race, where does the actual winner rank in the model's score list?
    winner_rank_dist = Counter()
    winner_in_top_n = defaultdict(int)
    total_races = 0
    winner_not_in_list = 0

    for e in period_entries:
        for r in e.get("races", []):
            pl = r.get("pick_list", [])
            if not pl:
                continue
            total_races += 1
            winner_nr = r.get("vinnare")

            # Sort picks by score descending
            sorted_picks = sorted(pl, key=lambda x: x.get("score", 0), reverse=True)
            found = False
            for rank, pick in enumerate(sorted_picks, 1):
                if pick["nr"] == winner_nr:
                    winner_rank_dist[rank] += 1
                    for n in range(rank, 16):
                        winner_in_top_n[n] += 1
                    found = True
                    break
            if not found:
                winner_not_in_list += 1

    print(f"\n--- {period} ---")
    print(f"  Total races analyzed: {total_races}")
    print(f"  Winner NOT in pick list: {winner_not_in_list} ({winner_not_in_list/total_races*100:.1f}%)")
    print(f"  Winner rank in pick list (by score):")
    for rank in sorted(winner_rank_dist.keys()):
        pct = winner_rank_dist[rank] / total_races * 100
        print(f"    Rank {rank}: {winner_rank_dist[rank]:>6} ({pct:.1f}%)")
    print(f"  Cumulative: Winner in top-N picks:")
    for n in [1, 2, 3, 4, 5]:
        if n in winner_in_top_n:
            pct = winner_in_top_n[n] / total_races * 100
            print(f"    Top {n}: {winner_in_top_n[n]:>6} ({pct:.1f}%)")

# ============================================================
# 8. FAVORITE vs VALUE PICK ANALYSIS
# ============================================================
print("\n\n" + "=" * 80)
print("8. FAVORITE vs VALUE PICK ANALYSIS (streck% distribution)")
print("=" * 80)

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    # Model's picks streck distribution vs winner's streck
    model_streck = []
    winner_streck = []
    correct_pick_streck = []
    missed_winner_streck = []

    for e in period_entries:
        for r in e.get("races", []):
            pl = r.get("pick_list", [])
            w_streck = r.get("vinnare_streck", 0)
            winner_nr = r.get("vinnare")

            if w_streck > 0:
                winner_streck.append(w_streck)

            for pick in pl:
                model_streck.append(pick.get("streck", 0))

            if r.get("ratt"):
                # Find the winning pick's streck
                for pick in pl:
                    if pick["nr"] == winner_nr:
                        correct_pick_streck.append(pick.get("streck", 0))
                        break
            else:
                if w_streck > 0:
                    missed_winner_streck.append(w_streck)

    avg_model = statistics.mean(model_streck) if model_streck else 0
    avg_winner = statistics.mean(winner_streck) if winner_streck else 0
    avg_correct = statistics.mean(correct_pick_streck) if correct_pick_streck else 0
    avg_missed = statistics.mean(missed_winner_streck) if missed_winner_streck else 0

    print(f"\n--- {period} ---")
    print(f"  Avg streck% of model picks: {avg_model:.1f}%")
    print(f"  Avg streck% of actual winners: {avg_winner:.1f}%")
    print(f"  Avg streck% of CORRECT model picks: {avg_correct:.1f}%")
    print(f"  Avg streck% of MISSED winners: {avg_missed:.1f}%")

    # Streck buckets for winners
    streck_buckets = defaultdict(lambda: {"correct": 0, "total": 0})
    for e in period_entries:
        for r in e.get("races", []):
            w_streck = r.get("vinnare_streck", 0)
            if w_streck <= 0:
                continue
            if w_streck <= 5:
                bucket = "0-5%"
            elif w_streck <= 15:
                bucket = "5-15%"
            elif w_streck <= 30:
                bucket = "15-30%"
            elif w_streck <= 50:
                bucket = "30-50%"
            else:
                bucket = "50%+"
            streck_buckets[bucket]["total"] += 1
            if r.get("ratt"):
                streck_buckets[bucket]["correct"] += 1

    print(f"  Winner streck% vs model catch rate:")
    for bucket in ["0-5%", "5-15%", "15-30%", "30-50%", "50%+"]:
        sb = streck_buckets[bucket]
        if sb["total"] == 0:
            continue
        acc = sb["correct"] / sb["total"] * 100
        print(f"    {bucket:<8}: caught {sb['correct']:>5}/{sb['total']:>5} = {acc:.1f}%")

# ============================================================
# 9. MARCH-APRIL 2026 FAILURE PATTERN ANALYSIS
# ============================================================
print("\n\n" + "=" * 80)
print("9. MAR-APR 2026 FAILURE PATTERNS")
print("=" * 80)

mar_apr = [e for e in entries if e["date"] >= "2026-03-01"]

# By avd (race position)
avd_stats = defaultdict(lambda: {"correct": 0, "total": 0})
for e in mar_apr:
    for r in e.get("races", []):
        avd = r.get("avd", 0)
        avd_stats[avd]["total"] += 1
        if r.get("ratt"):
            avd_stats[avd]["correct"] += 1

print(f"\n--- By Avdelning (race position) ---")
print(f"  {'Avd':>4} {'Correct':>8} {'Total':>8} {'Accuracy':>8}")
for avd in sorted(avd_stats.keys()):
    acc = avd_stats[avd]["correct"] / avd_stats[avd]["total"] * 100 if avd_stats[avd]["total"] > 0 else 0
    print(f"  {avd:>4} {avd_stats[avd]['correct']:>8} {avd_stats[avd]['total']:>8} {acc:>7.1f}%")

# Compare to historical avd accuracy
print(f"\n--- Historical comparison by Avdelning ---")
hist_avd = defaultdict(lambda: {"correct": 0, "total": 0})
for e in entries:
    if e["date"] >= "2026-03-01":
        continue
    for r in e.get("races", []):
        avd = r.get("avd", 0)
        hist_avd[avd]["total"] += 1
        if r.get("ratt"):
            hist_avd[avd]["correct"] += 1

print(f"  {'Avd':>4} {'Hist Acc':>10} {'Live Acc':>10} {'Delta':>8}")
for avd in sorted(set(list(avd_stats.keys()) + list(hist_avd.keys()))):
    h_acc = hist_avd[avd]["correct"] / hist_avd[avd]["total"] * 100 if hist_avd[avd]["total"] > 0 else 0
    l_acc = avd_stats[avd]["correct"] / avd_stats[avd]["total"] * 100 if avd_stats[avd]["total"] > 0 else 0
    delta = l_acc - h_acc
    print(f"  {avd:>4} {h_acc:>9.1f}% {l_acc:>9.1f}% {delta:>+7.1f}%")

# By method (volt/auto)
print(f"\n--- By Method ---")
for period_name, period_entries in [("Historical", [e for e in entries if e["date"] < "2026-03-01"]),
                                      ("Mar-Apr 2026", mar_apr)]:
    method_stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for e in period_entries:
        for r in e.get("races", []):
            m = r.get("metod", "unknown")
            method_stats[m]["total"] += 1
            if r.get("ratt"):
                method_stats[m]["correct"] += 1
    print(f"  {period_name}:")
    for m in sorted(method_stats.keys()):
        acc = method_stats[m]["correct"] / method_stats[m]["total"] * 100 if method_stats[m]["total"] > 0 else 0
        print(f"    {m:<10}: {method_stats[m]['correct']:>6}/{method_stats[m]['total']:>6} = {acc:.1f}%")

# By distance
print(f"\n--- By Distance ---")
for period_name, period_entries in [("Historical", [e for e in entries if e["date"] < "2026-03-01"]),
                                      ("Mar-Apr 2026", mar_apr)]:
    dist_stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for e in period_entries:
        for r in e.get("races", []):
            d = r.get("dist", 0)
            if d <= 0:
                continue
            # Bucket distances
            if d <= 1640:
                bucket = "short (<=1640)"
            elif d <= 2140:
                bucket = "medium (1641-2140)"
            elif d <= 2640:
                bucket = "long (2141-2640)"
            else:
                bucket = "marathon (>2640)"
            dist_stats[bucket]["total"] += 1
            if r.get("ratt"):
                dist_stats[bucket]["correct"] += 1
    print(f"  {period_name}:")
    for bucket in ["short (<=1640)", "medium (1641-2140)", "long (2141-2640)", "marathon (>2640)"]:
        ds = dist_stats[bucket]
        if ds["total"] == 0:
            continue
        acc = ds["correct"] / ds["total"] * 100
        print(f"    {bucket:<25}: {ds['correct']:>6}/{ds['total']:>6} = {acc:.1f}%")

# By track
print(f"\n--- By Track (Mar-Apr 2026, sorted by entry count) ---")
track_stats = defaultdict(lambda: {"correct": 0, "total": 0, "entries": 0, "hits": 0})
for e in mar_apr:
    track = e.get("track", "?")
    track_stats[track]["entries"] += 1
    track_stats[track]["hits"] += 1 if e["hit"] else 0
    for r in e.get("races", []):
        track_stats[track]["total"] += 1
        if r.get("ratt"):
            track_stats[track]["correct"] += 1

sorted_tracks = sorted(track_stats.items(), key=lambda x: x[1]["entries"], reverse=True)
print(f"  {'Track':<20} {'Entries':>8} {'Hits':>5} {'HitRate':>8} {'RaceAcc':>8}")
for track, ts in sorted_tracks[:20]:
    hr = ts["hits"] / ts["entries"] * 100 if ts["entries"] > 0 else 0
    ra = ts["correct"] / ts["total"] * 100 if ts["total"] > 0 else 0
    print(f"  {track:<20} {ts['entries']:>8} {ts['hits']:>5} {hr:>7.1f}% {ra:>7.1f}%")

# ============================================================
# 10. MONTHLY TREND — ROLLING PERFORMANCE
# ============================================================
print("\n\n" + "=" * 80)
print("10. MONTHLY PERFORMANCE TREND (2024-2026)")
print("=" * 80)

monthly = defaultdict(lambda: {"count": 0, "hits": 0, "cost": 0.0, "payout": 0.0,
                                "race_correct": 0, "race_total": 0, "confs": []})
for e in entries:
    ym = get_year_month(e["date"])
    if ym < "2024-01":
        continue
    monthly[ym]["count"] += 1
    monthly[ym]["hits"] += 1 if e["hit"] else 0
    monthly[ym]["cost"] += e["cost"]
    monthly[ym]["payout"] += e["payout"]
    monthly[ym]["confs"].append(e["avg_confidence"])
    for r in e.get("races", []):
        monthly[ym]["race_total"] += 1
        monthly[ym]["race_correct"] += 1 if r.get("ratt") else 0

print(f"  {'Month':<10} {'N':>5} {'Hits':>5} {'Hit%':>6} {'ROI%':>8} {'RaceAcc':>8} {'AvgConf':>8}")
for ym in sorted(monthly.keys()):
    m = monthly[ym]
    hr = m["hits"] / m["count"] * 100 if m["count"] > 0 else 0
    roi = (m["payout"] - m["cost"]) / m["cost"] * 100 if m["cost"] > 0 else 0
    ra = m["race_correct"] / m["race_total"] * 100 if m["race_total"] > 0 else 0
    ac = statistics.mean(m["confs"]) if m["confs"] else 0
    print(f"  {ym:<10} {m['count']:>5} {m['hits']:>5} {hr:>5.1f}% {roi:>+7.1f}% {ra:>7.1f}% {ac:>7.1f}")

# ============================================================
# 11. SCORE DISTRIBUTION ANALYSIS — LOOK FOR LEAKAGE IN SCORES
# ============================================================
print("\n\n" + "=" * 80)
print("11. SCORE DISTRIBUTION — DATA LEAKAGE INDICATORS")
print("=" * 80)

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    winner_scores = []  # Score of the pick that IS the winner (when model picked it)
    non_winner_scores = []  # Score of picks that are NOT the winner

    # Also check: does score correlate perfectly with being correct?
    score_90_plus_wins = 0
    score_90_plus_total = 0
    score_50_minus_wins = 0
    score_50_minus_total = 0

    for e in period_entries:
        for r in e.get("races", []):
            winner_nr = r.get("vinnare")
            for pick in r.get("pick_list", []):
                score = pick.get("score", 0)
                if pick["nr"] == winner_nr:
                    winner_scores.append(score)
                else:
                    non_winner_scores.append(score)

                if score >= 90:
                    score_90_plus_total += 1
                    if pick["nr"] == winner_nr:
                        score_90_plus_wins += 1
                elif score <= 50:
                    score_50_minus_total += 1
                    if pick["nr"] == winner_nr:
                        score_50_minus_wins += 1

    avg_w = statistics.mean(winner_scores) if winner_scores else 0
    avg_nw = statistics.mean(non_winner_scores) if non_winner_scores else 0
    s90_rate = score_90_plus_wins / score_90_plus_total * 100 if score_90_plus_total > 0 else 0
    s50_rate = score_50_minus_wins / score_50_minus_total * 100 if score_50_minus_total > 0 else 0

    print(f"\n--- {period} ---")
    print(f"  Avg score of WINNING picks: {avg_w:.1f} (n={len(winner_scores)})")
    print(f"  Avg score of NON-WINNING picks: {avg_nw:.1f} (n={len(non_winner_scores)})")
    print(f"  Score gap (winners - non-winners): {avg_w - avg_nw:.1f}")
    print(f"  Score >= 90 win rate: {score_90_plus_wins}/{score_90_plus_total} = {s90_rate:.1f}%")
    print(f"  Score <= 50 win rate: {score_50_minus_wins}/{score_50_minus_total} = {s50_rate:.1f}%")

# ============================================================
# 12. TACKNING (coverage) ANALYSIS
# ============================================================
print("\n\n" + "=" * 80)
print("12. TACKNING (COVERAGE %) vs ACCURACY")
print("=" * 80)

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    tack_correct = defaultdict(lambda: {"correct": 0, "total": 0})
    all_tack = []
    for e in period_entries:
        for r in e.get("races", []):
            tack = r.get("tackning", 0)
            all_tack.append(tack)
            if tack <= 30:
                bucket = "0-30%"
            elif tack <= 50:
                bucket = "30-50%"
            elif tack <= 70:
                bucket = "50-70%"
            elif tack <= 90:
                bucket = "70-90%"
            else:
                bucket = "90-100%"
            tack_correct[bucket]["total"] += 1
            if r.get("ratt"):
                tack_correct[bucket]["correct"] += 1

    avg_tack = statistics.mean(all_tack) if all_tack else 0
    print(f"\n--- {period} ---")
    print(f"  Avg tackning: {avg_tack:.1f}%")
    for bucket in ["0-30%", "30-50%", "50-70%", "70-90%", "90-100%"]:
        tc = tack_correct[bucket]
        if tc["total"] == 0:
            continue
        acc = tc["correct"] / tc["total"] * 100
        print(f"    {bucket:<10}: {tc['correct']:>6}/{tc['total']:>6} = {acc:.1f}%")

# ============================================================
# 13. STARTERS vs PICKS — FIELD SIZE IMPACT
# ============================================================
print("\n\n" + "=" * 80)
print("13. FIELD SIZE (STARTERS) vs ACCURACY")
print("=" * 80)

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    starter_acc = defaultdict(lambda: {"correct": 0, "total": 0})
    for e in period_entries:
        for r in e.get("races", []):
            s = r.get("startande", 0)
            if s <= 8:
                bucket = "small (<=8)"
            elif s <= 12:
                bucket = "medium (9-12)"
            else:
                bucket = "large (13+)"
            starter_acc[bucket]["total"] += 1
            if r.get("ratt"):
                starter_acc[bucket]["correct"] += 1

    print(f"\n--- {period} ---")
    for bucket in ["small (<=8)", "medium (9-12)", "large (13+)"]:
        sa = starter_acc[bucket]
        if sa["total"] == 0:
            continue
        acc = sa["correct"] / sa["total"] * 100
        print(f"    {bucket:<16}: {sa['correct']:>6}/{sa['total']:>6} = {acc:.1f}%")

# ============================================================
# 14. UPSET RISK vs CORRECTNESS
# ============================================================
print("\n\n" + "=" * 80)
print("14. UPSET RISK vs ACTUAL OUTCOME")
print("=" * 80)

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    upset_acc = defaultdict(lambda: {"correct": 0, "total": 0})
    for e in period_entries:
        for r in e.get("races", []):
            u = r.get("upset", 0)
            if u <= 10:
                bucket = "low (0-10)"
            elif u <= 25:
                bucket = "medium (11-25)"
            elif u <= 50:
                bucket = "high (26-50)"
            else:
                bucket = "extreme (51+)"
            upset_acc[bucket]["total"] += 1
            if r.get("ratt"):
                upset_acc[bucket]["correct"] += 1

    print(f"\n--- {period} ---")
    for bucket in ["low (0-10)", "medium (11-25)", "high (26-50)", "extreme (51+)"]:
        ua = upset_acc[bucket]
        if ua["total"] == 0:
            continue
        acc = ua["correct"] / ua["total"] * 100
        print(f"    {bucket:<20}: {ua['correct']:>6}/{ua['total']:>6} = {acc:.1f}%")

# ============================================================
# 15. CROSS-CHECK: Does model have access to future info?
# ============================================================
print("\n\n" + "=" * 80)
print("15. DATA LEAKAGE CROSS-CHECKS")
print("=" * 80)

# Check 1: Is the winner always in the pick list for historical data but not for live?
for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    winner_in_list = 0
    winner_not_in_list = 0
    perfect_rounds = 0
    total_rounds = len(period_entries)

    for e in period_entries:
        all_correct = True
        for r in e.get("races", []):
            winner_nr = r.get("vinnare")
            pick_nrs = [p["nr"] for p in r.get("pick_list", [])]
            if winner_nr in pick_nrs:
                winner_in_list += 1
            else:
                winner_not_in_list += 1
                all_correct = False
        if all_correct:
            perfect_rounds += 1

    total = winner_in_list + winner_not_in_list
    wil_rate = winner_in_list / total * 100 if total > 0 else 0
    perfect_rate = perfect_rounds / total_rounds * 100 if total_rounds > 0 else 0

    print(f"\n--- {period} ---")
    print(f"  Winner in pick list: {winner_in_list}/{total} = {wil_rate:.1f}%")
    print(f"  Winner NOT in pick list: {winner_not_in_list}/{total}")
    print(f"  Perfect rounds (all races correct): {perfect_rounds}/{total_rounds} = {perfect_rate:.1f}%")

# Check 2: Suspiciously high confidence for correct picks in historical
print("\n\n--- Confidence distribution for correct vs incorrect picks ---")
for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    high_conf_correct = 0
    high_conf_total = 0
    for e in period_entries:
        for r in e.get("races", []):
            if r.get("conf", 0) >= 80:
                high_conf_total += 1
                if r.get("ratt"):
                    high_conf_correct += 1

    rate = high_conf_correct / high_conf_total * 100 if high_conf_total > 0 else 0
    print(f"  {period}: High-conf (>=80) accuracy: {high_conf_correct}/{high_conf_total} = {rate:.1f}%")

# Check 3: Compare avg_confidence between hit and miss rounds
print("\n\n--- Avg confidence for HIT vs MISS rounds ---")
for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    hit_confs = [e["avg_confidence"] for e in period_entries if e["hit"]]
    miss_confs = [e["avg_confidence"] for e in period_entries if not e["hit"]]

    avg_hit = statistics.mean(hit_confs) if hit_confs else 0
    avg_miss = statistics.mean(miss_confs) if miss_confs else 0
    print(f"  {period}:")
    print(f"    HIT rounds avg confidence:  {avg_hit:.1f} (n={len(hit_confs)})")
    print(f"    MISS rounds avg confidence: {avg_miss:.1f} (n={len(miss_confs)})")
    print(f"    Gap: {avg_hit - avg_miss:.1f}")

# ============================================================
# 16. GAME TYPE ANALYSIS
# ============================================================
print("\n\n" + "=" * 80)
print("16. PERFORMANCE BY GAME TYPE")
print("=" * 80)

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    gt_stats = defaultdict(lambda: {"count": 0, "hits": 0, "cost": 0.0, "payout": 0.0})
    for e in period_entries:
        gt = e["game_type"]
        gt_stats[gt]["count"] += 1
        gt_stats[gt]["hits"] += 1 if e["hit"] else 0
        gt_stats[gt]["cost"] += e["cost"]
        gt_stats[gt]["payout"] += e["payout"]

    print(f"\n--- {period} ---")
    print(f"  {'Game':>6} {'N':>6} {'Hits':>5} {'Hit%':>7} {'ROI%':>8}")
    for gt in sorted(gt_stats.keys()):
        gs = gt_stats[gt]
        hr = gs["hits"] / gs["count"] * 100 if gs["count"] > 0 else 0
        roi = (gs["payout"] - gs["cost"]) / gs["cost"] * 100 if gs["cost"] > 0 else 0
        print(f"  {gt:>6} {gs['count']:>6} {gs['hits']:>5} {hr:>6.1f}% {roi:>+7.1f}%")

# ============================================================
# 17. PARTIAL CORRECT ANALYSIS
# ============================================================
print("\n\n" + "=" * 80)
print("17. PARTIAL CORRECT DISTRIBUTION")
print("=" * 80)

for period in ["PRE-2025 (historical)", "2025 (historical)", "Jan-Feb 2026 (transition)", "Mar-Apr 2026 (live)"]:
    period_entries = [e for e in entries if get_period(e["date"]) == period]
    if not period_entries:
        continue

    partial_dist = Counter()
    for e in period_entries:
        pc = e.get("partial_correct", e.get("num_correct", 0))
        nr = e.get("num_races", 7)
        partial_dist[(pc, nr)] += 1

    print(f"\n--- {period} ---")
    # Group by num_races
    by_nr = defaultdict(Counter)
    for (pc, nr), count in partial_dist.items():
        by_nr[nr][pc] += count

    for nr in sorted(by_nr.keys()):
        total = sum(by_nr[nr].values())
        print(f"  {nr}-race rounds (n={total}):")
        for pc in sorted(by_nr[nr].keys()):
            pct = by_nr[nr][pc] / total * 100
            marker = " <-- FULL HIT" if pc == nr else ""
            print(f"    {pc}/{nr} correct: {by_nr[nr][pc]:>5} ({pct:>5.1f}%){marker}")

print("\n\n" + "=" * 80)
print("ANALYSIS COMPLETE")
print("=" * 80)
