#!/usr/bin/env python3
"""
Hybrid Narrowing Simulation
============================
Simulates narrowing the last pick in races where the gap between the
second-to-last and last pick scores exceeds a threshold.

Narrowing a race: remove the last pick -> reduces cost by factor (N-1)/N
but risks losing the winner if they were that last pick.

Three scenarios:
  1. Baseline    - no narrowing at all
  2. Uniform 30  - gap >= 30 for all game types
  3. Hybrid      - per-game-type thresholds:
       V64: 10, V86: 10, GS75: 10, V75: 30, V85: 20
"""

import json
import math
from collections import defaultdict

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
with open('/Users/dennisdemirtok/trading/trav-agent/backlog.json', 'r') as f:
    data = json.load(f)

ENTRIES = data['entries']
STRATEGIES_META = data['strategies']

# Focus strategies
FOCUS_STRATEGIES = ['I_streck_1st', 'Q_dom_x_mktgap']

# Hybrid thresholds
HYBRID_THRESHOLDS = {
    'V64':  10,
    'V86':  10,
    'GS75': 10,
    'V75':  30,
    'V85':  20,
}

UNIFORM_THRESHOLD = 30

# Years of interest
YEARS = ['2021', '2022', '2023', '2024', '2025', '2026']

# ---------------------------------------------------------------------------
# Simulation engine
# ---------------------------------------------------------------------------

def simulate_entry(entry, threshold_map):
    """
    Given an entry and a threshold map (game_type -> gap_threshold, or None for baseline),
    simulate the narrowed coupon.

    Returns (new_cost, new_payout, original_cost, original_payout, narrowed_races_count)

    Logic:
    - For each race with picks >= 2, compute gap = score[-2] - score[-1]
    - If gap >= threshold, narrow that race (remove last pick)
      - new_picks = picks - 1
      - If the winner was the last pick (vinnare == pick_list[-1]['nr'] and ratt==True),
        then narrowing causes that race to miss -> entire coupon loses full hit
    - Recompute cost = product of new picks per race
    - Recompute payout:
      - If all races still correct -> payout scales by (original_rows / new_rows) * payout_per_row
        Wait, that's not right. The payout_per_row is per winning row in the pool.
        If we narrow, we have fewer rows but each winning row pays the same.
        Actually: payout = winning_rows * payout_per_row
        If we narrow a race where we still have the winner, winning_rows changes.
        
    Actually, let me think about this more carefully:
    - Original: rows = product of all picks. winning_rows based on how many combos have all correct.
    - If we narrow a race and still have the winner in that race, the winning rows
      for that race's contribution halve (well, reduce by factor).
    - But payout_per_row is fixed by the pool, not by our coupon.
    
    Simpler approach:
    - payout_per_row is the pool payout per correct row (fixed, independent of our coupon)
    - winning_rows = product over races of (how many of our picks in that race are the winner)
      Usually 0 or 1 per race. If all races have exactly 1 winner among picks -> winning_rows = 1
    - If we narrow and keep the winner -> winning_rows unchanged = same payout
    - If we narrow and lose the winner -> that race has 0 correct -> winning_rows = 0 -> payout = 0
    - Cost changes based on the product of picks
    
    For partial hits (num_correct < num_races but payout > 0):
    - Some pool types pay for partial correctness
    - We need to check which races we got right and whether narrowing affects any of them
    """
    
    gt = entry['game_type']
    races = entry['races']
    original_cost = entry['cost']
    original_payout = entry['payout']
    
    if threshold_map is None:
        # Baseline - no narrowing
        return original_cost, original_payout, original_cost, original_payout, 0
    
    threshold = threshold_map.get(gt)
    if threshold is None:
        return original_cost, original_payout, original_cost, original_payout, 0
    
    # Determine which races to narrow
    new_picks_list = []
    lost_correct_race = False
    narrowed_count = 0
    
    for r in races:
        n = r['picks']
        if n <= 1:
            # Can't narrow
            new_picks_list.append(max(n, 1))  # treat 0 as 1 for cost calc below
            continue
        
        pl = r['pick_list']
        if len(pl) < 2:
            new_picks_list.append(n)
            continue
        
        gap = pl[-2]['score'] - pl[-1]['score']
        
        if gap >= threshold:
            # Narrow this race
            narrowed_count += 1
            new_n = n - 1
            new_picks_list.append(new_n)
            
            # Did we lose the winner?
            if r['ratt']:
                last_nr = pl[-1]['nr']
                if r['vinnare'] == last_nr:
                    lost_correct_race = True
        else:
            new_picks_list.append(n)
    
    # Compute new cost
    # Handle 0-pick races: they contribute 0 to the product effectively
    # But in the original data, cost = product of picks (where picks > 0)
    # Let's just compute product of all picks, treating 0 as not contributing
    has_zero = any(r['picks'] == 0 for r in races)
    if has_zero:
        # Entry with 0-pick race - can't really play this
        new_cost = original_cost  # keep as-is
        new_payout = original_payout
        return new_cost, new_payout, original_cost, original_payout, 0
    
    new_cost = 1
    for np in new_picks_list:
        new_cost *= np
    new_cost = float(new_cost)
    
    # Compute new payout
    if original_payout == 0:
        new_payout = 0.0
    elif lost_correct_race:
        # We lost a race that was previously correct
        # Need to figure out new num_correct
        # For simplicity: recount correct races after narrowing
        new_correct = 0
        for i, r in enumerate(races):
            if r['picks'] == 0:
                continue
            if r['ratt']:
                pl = r['pick_list']
                if len(pl) >= 2:
                    gap = pl[-2]['score'] - pl[-1]['score']
                    if gap >= threshold and r['vinnare'] == pl[-1]['nr']:
                        # This race was narrowed and we lost the winner
                        continue
                new_correct += 1
            # If not ratt, doesn't change
        
        old_correct = entry['num_correct']
        
        if new_correct == old_correct:
            # Didn't actually lose anything (edge case)
            new_payout = original_payout
        elif new_correct < old_correct:
            # Lost some correct races
            # For full-hit games (V64, V75, V86, V85, GS75): 
            # you typically need all correct for full payout
            # Partial payouts exist for some game types
            # Simplification: if we had a full hit and lost a race, check if partial pays
            # The payout_per_row field tells us what each winning row paid
            # For partial: the entry already accounts for partial payouts
            
            # Best approach: if the original was a full hit (num_correct == num_races)
            # and we lose a race, we drop to partial which may or may not pay
            # For a conservative sim, if we lose any correct race from a full hit,
            # assume payout = 0 (since partial payouts are much smaller and hard to predict)
            
            # Actually, let's handle this properly:
            # - If original was NOT a full hit, payout was already partial
            #   Losing more races likely drops payout to 0
            # - If original WAS a full hit, losing a race drops to partial
            #   The partial payout is unpredictable without pool data
            
            # Conservative: set payout to 0 when we lose any correct race
            new_payout = 0.0
        else:
            new_payout = original_payout
    else:
        # We didn't lose any correct race
        # Payout stays the same (same winning rows, same payout per row)
        new_payout = original_payout
    
    return new_cost, new_payout, original_cost, original_payout, narrowed_count


def run_simulation(entries, threshold_map, label):
    """Run simulation across all entries with given thresholds."""
    results = {
        'label': label,
        'total_cost': 0,
        'total_payout': 0,
        'orig_cost': 0,
        'orig_payout': 0,
        'n_entries': 0,
        'n_narrowed_entries': 0,
        'total_narrowed_races': 0,
        'payouts': [],  # for robust ROI
        'by_gt': defaultdict(lambda: {'cost': 0, 'payout': 0, 'orig_cost': 0, 'orig_payout': 0, 'n': 0, 'payouts': []}),
        'by_year': defaultdict(lambda: {'cost': 0, 'payout': 0, 'orig_cost': 0, 'orig_payout': 0, 'n': 0, 'payouts': []}),
        'by_strat': defaultdict(lambda: {'cost': 0, 'payout': 0, 'orig_cost': 0, 'orig_payout': 0, 'n': 0, 'payouts': []}),
        'by_year_gt': defaultdict(lambda: {'cost': 0, 'payout': 0, 'n': 0, 'payouts': []}),
    }
    
    for e in entries:
        new_cost, new_payout, orig_cost, orig_payout, narrowed_races = simulate_entry(e, threshold_map)
        
        year = e['date'][:4]
        gt = e['game_type']
        strat = e['strategy']
        
        results['total_cost'] += new_cost
        results['total_payout'] += new_payout
        results['orig_cost'] += orig_cost
        results['orig_payout'] += orig_payout
        results['n_entries'] += 1
        results['payouts'].append(new_payout)
        if narrowed_races > 0:
            results['n_narrowed_entries'] += 1
            results['total_narrowed_races'] += narrowed_races
        
        for key, val in [('by_gt', gt), ('by_year', year), ('by_strat', strat)]:
            results[key][val]['cost'] += new_cost
            results[key][val]['payout'] += new_payout
            results[key][val]['orig_cost'] += orig_cost
            results[key][val]['orig_payout'] += orig_payout
            results[key][val]['n'] += 1
            results[key][val]['payouts'].append(new_payout)
        
        ygt_key = f"{year}_{gt}"
        results['by_year_gt'][ygt_key]['cost'] += new_cost
        results['by_year_gt'][ygt_key]['payout'] += new_payout
        results['by_year_gt'][ygt_key]['n'] += 1
        results['by_year_gt'][ygt_key]['payouts'].append(new_payout)
    
    return results


def robust_roi(payouts, cost, top_pct=0.01):
    """ROI after removing top X% of payouts."""
    if cost == 0 or not payouts:
        return 0.0
    sorted_p = sorted(payouts, reverse=True)
    n_remove = max(1, int(len(sorted_p) * top_pct))
    removed = sorted_p[n_remove:]
    removed_payouts_sum = sum(removed)
    # Also remove proportional cost
    removed_cost = cost * (len(removed) / len(sorted_p))
    if removed_cost == 0:
        return 0.0
    return (removed_payouts_sum - removed_cost) / removed_cost * 100


def roi(payout, cost):
    if cost == 0:
        return 0.0
    return (payout - cost) / cost * 100


# ---------------------------------------------------------------------------
# Run simulations
# ---------------------------------------------------------------------------

# Filter to focus strategies for strategy-specific analysis
all_entries = ENTRIES
focus_entries = [e for e in ENTRIES if e['strategy'] in FOCUS_STRATEGIES]

print("=" * 100)
print("HYBRID NARROWING SIMULATION")
print("=" * 100)
print(f"Total entries: {len(all_entries)}")
print(f"Focus strategy entries: {len(focus_entries)}")
print(f"Date range: {min(e['date'] for e in all_entries)} to {max(e['date'] for e in all_entries)}")
print()

scenarios = [
    ("Baseline (no narrowing)", None),
    ("Uniform gap >= 30", {'V64': 30, 'V86': 30, 'GS75': 30, 'V75': 30, 'V85': 30}),
    ("Hybrid thresholds", HYBRID_THRESHOLDS),
]

all_results = {}

for label, thresholds in scenarios:
    res = run_simulation(all_entries, thresholds, label)
    all_results[label] = res

# ---------------------------------------------------------------------------
# Print overall comparison
# ---------------------------------------------------------------------------
print("=" * 100)
print("OVERALL COMPARISON (ALL STRATEGIES)")
print("=" * 100)
print(f"{'Scenario':<30} {'Entries':>8} {'Narrowed':>8} {'Cost':>14} {'Payout':>14} {'Netto':>14} {'ROI':>8} {'Robust ROI':>10}")
print("-" * 100)
for label, _, in scenarios:
    r = all_results[label]
    netto = r['total_payout'] - r['total_cost']
    r_roi = roi(r['total_payout'], r['total_cost'])
    r_rroi = robust_roi(r['payouts'], r['total_cost'])
    print(f"{r['label']:<30} {r['n_entries']:>8} {r['n_narrowed_entries']:>8} {r['total_cost']:>14,.0f} {r['total_payout']:>14,.0f} {netto:>14,.0f} {r_roi:>7.1f}% {r_rroi:>9.1f}%")

# Cost savings
baseline_cost = all_results["Baseline (no narrowing)"]['total_cost']
for label in ["Uniform gap >= 30", "Hybrid thresholds"]:
    r = all_results[label]
    saving = baseline_cost - r['total_cost']
    pct = saving / baseline_cost * 100 if baseline_cost else 0
    print(f"\n  {label}: Cost saving = {saving:,.0f} ({pct:.1f}%)")

# ---------------------------------------------------------------------------
# Per game type breakdown
# ---------------------------------------------------------------------------
print("\n" + "=" * 100)
print("PER GAME TYPE BREAKDOWN")
print("=" * 100)

game_types = sorted(set(e['game_type'] for e in all_entries))
for gt in game_types:
    print(f"\n--- {gt} ---")
    print(f"  {'Scenario':<30} {'N':>6} {'Cost':>12} {'Payout':>12} {'Netto':>12} {'ROI':>8} {'Robust ROI':>10}")
    print(f"  {'-'*82}")
    for label, _ in scenarios:
        r = all_results[label]
        d = r['by_gt'].get(gt, {'cost': 0, 'payout': 0, 'n': 0, 'payouts': []})
        netto = d['payout'] - d['cost']
        r_roi = roi(d['payout'], d['cost'])
        r_rroi = robust_roi(d['payouts'], d['cost'])
        print(f"  {label:<30} {d['n']:>6} {d['cost']:>12,.0f} {d['payout']:>12,.0f} {netto:>12,.0f} {r_roi:>7.1f}% {r_rroi:>9.1f}%")

# ---------------------------------------------------------------------------
# Per year breakdown
# ---------------------------------------------------------------------------
print("\n" + "=" * 100)
print("PER YEAR BREAKDOWN")
print("=" * 100)

for year in YEARS:
    print(f"\n--- {year} ---")
    print(f"  {'Scenario':<30} {'N':>6} {'Cost':>12} {'Payout':>12} {'Netto':>12} {'ROI':>8} {'Robust ROI':>10}")
    print(f"  {'-'*82}")
    for label, _ in scenarios:
        r = all_results[label]
        d = r['by_year'].get(year, {'cost': 0, 'payout': 0, 'n': 0, 'payouts': []})
        netto = d['payout'] - d['cost']
        r_roi = roi(d['payout'], d['cost'])
        r_rroi = robust_roi(d['payouts'], d['cost'])
        print(f"  {label:<30} {d['n']:>6} {d['cost']:>12,.0f} {d['payout']:>12,.0f} {netto:>12,.0f} {r_roi:>7.1f}% {r_rroi:>9.1f}%")

# ---------------------------------------------------------------------------
# Per strategy breakdown (focus strategies)
# ---------------------------------------------------------------------------
print("\n" + "=" * 100)
print("PER STRATEGY BREAKDOWN (FOCUS STRATEGIES)")
print("=" * 100)

for strat in FOCUS_STRATEGIES:
    print(f"\n--- {strat} ---")
    print(f"  {'Scenario':<30} {'N':>6} {'Cost':>12} {'Payout':>12} {'Netto':>12} {'ROI':>8} {'Robust ROI':>10}")
    print(f"  {'-'*82}")
    for label, _ in scenarios:
        r = all_results[label]
        d = r['by_strat'].get(strat, {'cost': 0, 'payout': 0, 'n': 0, 'payouts': []})
        netto = d['payout'] - d['cost']
        r_roi = roi(d['payout'], d['cost'])
        r_rroi = robust_roi(d['payouts'], d['cost'])
        print(f"  {label:<30} {d['n']:>6} {d['cost']:>12,.0f} {d['payout']:>12,.0f} {netto:>12,.0f} {r_roi:>7.1f}% {r_rroi:>9.1f}%")

# ---------------------------------------------------------------------------
# CURVE-FIT CHECK: Year-by-year profitability
# ---------------------------------------------------------------------------
print("\n" + "=" * 100)
print("CURVE-FIT CHECK: YEAR-BY-YEAR PROFITABILITY")
print("=" * 100)

print(f"\n{'Year':<6}", end="")
for label, _ in scenarios:
    short = label.split("(")[0].strip()[:20]
    print(f"  {'ROI':>8} {'RobROI':>8} {'Prof?':>6}", end="")
print()
print("-" * 84)

profitability = {label: {'profitable_years': 0, 'total_years': 0} for label, _ in scenarios}

for year in YEARS:
    print(f"{year:<6}", end="")
    for label, _ in scenarios:
        r = all_results[label]
        d = r['by_year'].get(year, {'cost': 0, 'payout': 0, 'n': 0, 'payouts': []})
        if d['n'] == 0:
            print(f"  {'N/A':>8} {'N/A':>8} {'N/A':>6}", end="")
            continue
        r_roi = roi(d['payout'], d['cost'])
        r_rroi = robust_roi(d['payouts'], d['cost'])
        is_prof = r_rroi > 0
        profitability[label]['total_years'] += 1
        if is_prof:
            profitability[label]['profitable_years'] += 1
        marker = "YES" if is_prof else "NO"
        print(f"  {r_roi:>7.1f}% {r_rroi:>7.1f}% {marker:>6}", end="")
    print()

print()
print("Profitable years summary (based on Robust ROI > 0):")
for label, _ in scenarios:
    p = profitability[label]
    print(f"  {label:<30}: {p['profitable_years']}/{p['total_years']} years profitable")

# ---------------------------------------------------------------------------
# Detailed: Hybrid per year per game type
# ---------------------------------------------------------------------------
print("\n" + "=" * 100)
print("HYBRID: PER YEAR x PER GAME TYPE (ROI / Robust ROI)")
print("=" * 100)

hybrid_res = all_results["Hybrid thresholds"]

header = f"{'Year':<6}"
for gt in game_types:
    header += f"  {gt:>20}"
print(header)
print("-" * (6 + 22 * len(game_types)))

for year in YEARS:
    line = f"{year:<6}"
    for gt in game_types:
        key = f"{year}_{gt}"
        d = hybrid_res['by_year_gt'].get(key, {'cost': 0, 'payout': 0, 'n': 0, 'payouts': []})
        if d['n'] == 0:
            line += f"  {'N/A':>20}"
        else:
            r_roi = roi(d['payout'], d['cost'])
            r_rroi = robust_roi(d['payouts'], d['cost'])
            line += f"  {r_roi:>7.1f}/{r_rroi:>7.1f}%"
    print(line)

# ---------------------------------------------------------------------------
# Narrowing impact analysis
# ---------------------------------------------------------------------------
print("\n" + "=" * 100)
print("NARROWING IMPACT ANALYSIS (HYBRID)")
print("=" * 100)

# How many entries were narrowed, how many lost hits
hybrid_thresholds = HYBRID_THRESHOLDS
narrowed_stats = defaultdict(lambda: {'total': 0, 'narrowed': 0, 'lost_hits': 0, 'kept_hits': 0, 
                                        'races_narrowed': 0, 'races_lost_winner': 0})

for e in all_entries:
    gt = e['game_type']
    threshold = hybrid_thresholds.get(gt)
    if threshold is None:
        continue
    
    narrowed_stats[gt]['total'] += 1
    any_narrowed = False
    lost_any = False
    
    for r in e['races']:
        if r['picks'] < 2 or len(r['pick_list']) < 2:
            continue
        gap = r['pick_list'][-2]['score'] - r['pick_list'][-1]['score']
        if gap >= threshold:
            any_narrowed = True
            narrowed_stats[gt]['races_narrowed'] += 1
            if r['ratt'] and r['vinnare'] == r['pick_list'][-1]['nr']:
                narrowed_stats[gt]['races_lost_winner'] += 1
                lost_any = True
    
    if any_narrowed:
        narrowed_stats[gt]['narrowed'] += 1
    if lost_any:
        narrowed_stats[gt]['lost_hits'] += 1
    elif e['hit'] and any_narrowed:
        narrowed_stats[gt]['kept_hits'] += 1

print(f"\n{'Game':<8} {'Total':>8} {'Narrowed':>10} {'Races Nar':>10} {'Races Lost':>11} {'Entries Lost':>12} {'Entries Kept':>12}")
print("-" * 75)
for gt in game_types:
    s = narrowed_stats[gt]
    print(f"{gt:<8} {s['total']:>8} {s['narrowed']:>10} {s['races_narrowed']:>10} {s['races_lost_winner']:>11} {s['lost_hits']:>12} {s['kept_hits']:>12}")

# ---------------------------------------------------------------------------
# Strategy-specific year-by-year for focus strategies
# ---------------------------------------------------------------------------
print("\n" + "=" * 100)
print("FOCUS STRATEGY YEAR-BY-YEAR (HYBRID vs BASELINE)")
print("=" * 100)

for strat in FOCUS_STRATEGIES:
    strat_entries = [e for e in all_entries if e['strategy'] == strat]
    
    print(f"\n=== {strat} ({len(strat_entries)} entries) ===")
    
    baseline_res = run_simulation(strat_entries, None, "Baseline")
    hybrid_res_s = run_simulation(strat_entries, HYBRID_THRESHOLDS, "Hybrid")
    
    print(f"  {'Year':<6} | {'--- Baseline ---':^36} | {'--- Hybrid ---':^36} | {'Delta':>8}")
    print(f"  {'':6} | {'Cost':>10} {'Payout':>10} {'ROI':>8} {'RobROI':>8} | {'Cost':>10} {'Payout':>10} {'ROI':>8} {'RobROI':>8} | {'RobROI':>8}")
    print(f"  {'-'*100}")
    
    for year in YEARS:
        db = baseline_res['by_year'].get(year, {'cost': 0, 'payout': 0, 'n': 0, 'payouts': []})
        dh = hybrid_res_s['by_year'].get(year, {'cost': 0, 'payout': 0, 'n': 0, 'payouts': []})
        
        if db['n'] == 0:
            print(f"  {year:<6} | {'N/A':^36} | {'N/A':^36} |")
            continue
        
        b_roi = roi(db['payout'], db['cost'])
        b_rroi = robust_roi(db['payouts'], db['cost'])
        h_roi = roi(dh['payout'], dh['cost'])
        h_rroi = robust_roi(dh['payouts'], dh['cost'])
        delta = h_rroi - b_rroi
        
        print(f"  {year:<6} | {db['cost']:>10,.0f} {db['payout']:>10,.0f} {b_roi:>7.1f}% {b_rroi:>7.1f}% | {dh['cost']:>10,.0f} {dh['payout']:>10,.0f} {h_roi:>7.1f}% {h_rroi:>7.1f}% | {delta:>+7.1f}%")

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
print("\n" + "=" * 100)
print("FINAL SUMMARY")
print("=" * 100)

baseline = all_results["Baseline (no narrowing)"]
hybrid = all_results["Hybrid thresholds"]
uniform = all_results["Uniform gap >= 30"]

b_rroi = robust_roi(baseline['payouts'], baseline['total_cost'])
h_rroi = robust_roi(hybrid['payouts'], hybrid['total_cost'])
u_rroi = robust_roi(uniform['payouts'], uniform['total_cost'])

print(f"""
  Baseline Robust ROI:     {b_rroi:>+.1f}%
  Uniform30 Robust ROI:    {u_rroi:>+.1f}%  (delta: {u_rroi - b_rroi:>+.1f}pp)
  Hybrid Robust ROI:       {h_rroi:>+.1f}%  (delta: {h_rroi - b_rroi:>+.1f}pp)

  Cost savings (Hybrid):   {baseline['total_cost'] - hybrid['total_cost']:>,.0f} kr ({(baseline['total_cost'] - hybrid['total_cost'])/baseline['total_cost']*100:.1f}%)
  Cost savings (Uniform):  {baseline['total_cost'] - uniform['total_cost']:>,.0f} kr ({(baseline['total_cost'] - uniform['total_cost'])/baseline['total_cost']*100:.1f}%)
  
  Payout impact (Hybrid):  {hybrid['total_payout'] - baseline['total_payout']:>+,.0f} kr
  Payout impact (Uniform): {uniform['total_payout'] - baseline['total_payout']:>+,.0f} kr

  Hybrid profitable years:  {profitability['Hybrid thresholds']['profitable_years']}/{profitability['Hybrid thresholds']['total_years']}
  Baseline profitable years: {profitability['Baseline (no narrowing)']['profitable_years']}/{profitability['Baseline (no narrowing)']['total_years']}
""")

# Curve-fit verdict
hybrid_prof_years = profitability['Hybrid thresholds']['profitable_years']
hybrid_total_years = profitability['Hybrid thresholds']['total_years']
baseline_prof_years = profitability['Baseline (no narrowing)']['profitable_years']

if hybrid_prof_years >= hybrid_total_years * 0.8:
    verdict = "LOW curve-fit risk - hybrid profitable across most years"
elif hybrid_prof_years >= hybrid_total_years * 0.5:
    verdict = "MODERATE curve-fit risk - hybrid profitable in some years"
else:
    verdict = "HIGH curve-fit risk - hybrid only profitable in few years"

if hybrid_prof_years <= baseline_prof_years:
    verdict += " | WARNING: hybrid does NOT improve year-count over baseline"
else:
    verdict += f" | hybrid improves profitable years: {baseline_prof_years} -> {hybrid_prof_years}"

print(f"  CURVE-FIT VERDICT: {verdict}")
print()
